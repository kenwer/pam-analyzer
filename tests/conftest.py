"""Shared test fixtures.

`qtbot` is auto-provided by pytest-qt. We force the offscreen platform so
headless CI works the same as a developer laptop.
"""

import gc
import os
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from PySide6.QtCore import QCoreApplication, QThread
from PySide6.QtQuickWidgets import QQuickWidget

from pam_analyzer.domain.audio_import import DetectedCard, ImportSource
from pam_analyzer.infrastructure.birdnet_2_4_runner import MODEL_KEY as MODEL_KEY_V2_4
from pam_analyzer.infrastructure.birdnet_runner import MODEL_KEY as MODEL_KEY_V3_0

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# The model key a run writes by default
DEFAULT_MODEL_KEY = MODEL_KEY_V2_4

# The other engine the app ships. Tests whose subject is the model selector
# or cross-engine CSV coexistence should name this one.
ALT_MODEL_KEY = MODEL_KEY_V3_0

# Keys the app can still read but no longer writes. Only tests whose subject
# is that continuity should name one of these, and they should say so.
RETIRED_MODEL_KEYS = ("Perch-2.0",)


class FakeScanner:
    """Stand-in for PsutilSdCardScanner. Offers whatever cards a test sets and records ejects."""

    def __init__(self, cards: list[DetectedCard] | None = None) -> None:
        self.cards: list[DetectedCard] = list(cards or [])
        self.ejected: list[DetectedCard] = []

    def scan(self, name_pattern: str) -> list[DetectedCard]:
        return list(self.cards)

    def eject(self, card: DetectedCard) -> None:
        self.ejected.append(card)


def folder_card(root: Path, name: str) -> DetectedCard:
    """A folder-import card holding one short recording."""
    folder = root / name
    folder.mkdir()
    sf.write(folder / "20260619_073000.WAV", np.zeros(4800, dtype=np.int16), 48_000, subtype="PCM_16")
    return DetectedCard(name=name, mountpoint=folder, device="", source=ImportSource.FOLDER)


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
            obj.quit()
            obj.wait()
