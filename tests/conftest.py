"""Shared test fixtures.

`qtbot` is auto-provided by pytest-qt. We force the offscreen platform so
headless CI works the same as a developer laptop.
"""

import gc
import os
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from PySide6.QtCore import QCoreApplication, QObject, QThread
from PySide6.QtQuickWidgets import QQuickWidget

from pam_analyzer.domain import (
    AnalysisProgress,
    AnalysisRunResult,
    AnalysisSettings,
    AudioInventory,
    Campaign,
    CampaignInventory,
    ModelInfo,
    RunStatus,
)
from pam_analyzer.domain.audio_import import (
    CardImportResult,
    ConflictChoice,
    DetectedCard,
    ImportProgress,
    ImportSource,
)
from pam_analyzer.infrastructure import AudioImporter
from pam_analyzer.infrastructure.birdnet_2_4_runner import MODEL_KEY as MODEL_KEY_V2_4
from pam_analyzer.infrastructure.birdnet_3_0_runner import MODEL_KEY as MODEL_KEY_V3_0
from pam_analyzer.infrastructure.update.feed import Update
from pam_analyzer.infrastructure.update.install import LinuxAppInstall

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Models come from `poe prewarm`, so a lib download during tests means a file it
# missed. This fails that test with the URL instead of fetching it silently.
os.environ.setdefault("BIRDNET_OFFLINE", "1")

# The model key a run writes by default
DEFAULT_MODEL_KEY = MODEL_KEY_V2_4

# The other engine the app ships. Tests whose subject is the model selector
# or cross-engine CSV coexistence should name this one.
ALT_MODEL_KEY = MODEL_KEY_V3_0
FAKE_MODEL_INFO = ModelInfo("Fake 1.0", "https://example.org/fake", "CC0 1.0")

# Keys the app can still read but no longer writes. Only tests whose subject
# is that continuity should name one of these, and they should say so.
RETIRED_MODEL_KEYS = ("BirdNET-3.0-preview3.1",)


def wait_until_cancelled(is_cancelled: Callable[[], bool]) -> bool:
    """Block a fake worker until cancelled. Gives up after 10 s so a regression fails instead of hanging."""
    deadline = time.monotonic() + 10
    while not is_cancelled():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


class FakeRunner:
    model_key = DEFAULT_MODEL_KEY
    model_info = FAKE_MODEL_INFO

    def missing_download_bytes(self) -> int:
        return 0

    def install_model(self, progress, cancelled) -> None:
        pass

    def count_audio_files(self, campaign_dir: Path) -> int:
        return 0

    def available_locales(self) -> list[str]:
        return ["en"]

    def run(
        self,
        *,
        campaigns: list[Campaign],
        settings: AnalysisSettings,
        preferred_lang: str,
        progress: AnalysisProgress,
    ) -> AnalysisRunResult:
        return AnalysisRunResult(status=RunStatus.COMPLETED)


class FakeScanner:
    """Stand-in for PsutilSdCardScanner. Offers whatever cards a test sets and records ejects."""

    def __init__(self, cards: list[DetectedCard] | None = None) -> None:
        self.cards: list[DetectedCard] = list(cards or [])
        self.ejected: list[DetectedCard] = []

    def scan(self, name_pattern: str) -> list[DetectedCard]:
        return list(self.cards)

    def eject(self, card: DetectedCard) -> None:
        self.ejected.append(card)


class ImporterUntilCancelled(AudioImporter):
    """Blocks each card's copy until cancelled and records which cards it started."""

    def __init__(self) -> None:
        super().__init__()
        self.imported: list[str] = []

    def import_card(
        self,
        card: DetectedCard,
        files: list[Path],
        dest_dir: Path,
        resolutions: dict[str, ConflictChoice],
        identical: tuple[str, ...],
        *,
        progress: Callable[[ImportProgress], None],
        is_cancelled: Callable[[], bool],
        clear_after: bool,
    ) -> CardImportResult:
        self.imported.append(card.name)
        wait_until_cancelled(is_cancelled)
        return CardImportResult(
            card=card, files_copied=0, files_skipped=0, bytes_copied=0, elapsed=0.0,
            error=CardImportResult.CANCELLED, dest_dir=None,
        )


def campaign_of(inventory: AudioInventory, name: str) -> CampaignInventory:
    campaign = inventory.for_campaign(name)
    assert campaign is not None
    return campaign


def folder_card(root: Path, name: str) -> DetectedCard:
    """A folder-import card holding one short recording."""
    folder = root / name
    folder.mkdir()
    sf.write(folder / "20260619_073000.WAV", np.zeros(4800, dtype=np.int16), 48_000, subtype="PCM_16")
    return DetectedCard(name=name, mountpoint=folder, device="", source=ImportSource.FOLDER)


# A release the update tests offer over version 0.8.0
UPDATE = Update("0.9.0", "notes", "https://example.org/r", "a.AppImage", "https://example.org/a", 10, "0" * 64)


def linux_install(tmp_path: Path) -> LinuxAppInstall:
    return LinuxAppInstall(tmp_path / "PAM-Analyzer.AppImage", tmp_path / "stage")


def stop_worker_threads(owner: QObject) -> None:
    """Stop the worker threads under owner the way MainWindow.closeEvent does."""
    for thread in owner.findChildren(QThread):
        thread.requestInterruption()  # stops a busy run()
        thread.quit()  # stops an idle event loop
        thread.wait()


def _drain_qt_events() -> None:
    app = QCoreApplication.instance()
    if app is not None:
        app.processEvents()


# pytest-qt processes pending events after setup and call, but outside
# pytest's capture, so Qt warnings from deferred events reach the terminal.
# trylast puts these wrappers inside the capture, where they drain first.
@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_setup(item):
    result = yield
    _drain_qt_events()
    return result


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_call(item):
    result = yield
    _drain_qt_events()
    return result


@pytest.fixture(autouse=True)
def _no_live_map(monkeypatch) -> None:
    """Keep MapPickerWidget from loading its QML.

    The real map fetches tiles from OpenStreetMap as soon as it exists, so
    every test that builds a campaign panel would hit their server. Without
    the QML the widget has no root object and its methods do nothing.
    """
    monkeypatch.setattr(QQuickWidget, "setSource", lambda self, url: None)


@pytest.fixture(autouse=True)
def _stop_qt_threads():
    """Stop every QThread a test left running before the next test runs.

    Widgets such as SpectrogramWidget start a worker QThread that the running
    app stops via QApplication.aboutToQuit. Under pytest there is no event loop
    and that signal never fires, so each test's widgets keep their threads
    running. When such a widget is later garbage-collected (mid-run, or by
    PySide6's atexit handler at shutdown) its QThread C++ object is destroyed,
    and ~QThread on a still-running thread calls abort(). Quitting the threads
    after each test, while the interpreter is healthy, avoids that crash.
    """
    yield
    for obj in gc.get_objects():
        if not isinstance(obj, QThread):
            continue
        try:
            running = obj.isRunning()
        except RuntimeError:
            continue  # underlying C++ object already gone
        if running:
            # A test that failed mid-run leaves its worker busy in run(), which
            # only an interruption request stops. quit() alone deadlocks wait().
            obj.requestInterruption()
            obj.quit()
            obj.wait()
