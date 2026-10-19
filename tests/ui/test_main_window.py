"""pytest-qt tests for MainWindow behaviour that spans several panels."""

import threading
from pathlib import Path

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QCloseEvent

from pam_analyzer.domain import AnalysisRunResult, Campaign, FilterMode, Project, RunStatus, paths
from pam_analyzer.infrastructure import AudioImporter
from pam_analyzer.ui import main_window
from pam_analyzer.ui.app_state import AppState
from pam_analyzer.ui.main_window import MainWindow
from pam_analyzer.ui.settings import AppSettings
from pam_analyzer.widgets.spectrogram_widget import SpectrogramWidget
from pam_analyzer.workers import ImportOrchestrator, audio_inventory_refresher, project_load_worker
from tests.conftest import (
    DEFAULT_MODEL_KEY,
    FakeScanner,
    ImporterUntilCancelled,
    folder_card,
    wait_until_cancelled,
)


class _FakeRunner:
    model_key = DEFAULT_MODEL_KEY

    def count_audio_files(self, _path: Path) -> int:
        return 0

    def available_locales(self) -> list[str]:
        return ["en"]

    def run(self, **kwargs) -> AnalysisRunResult:
        return AnalysisRunResult(status=RunStatus.COMPLETED)


class _RecordingWindow(MainWindow):
    """Records an accepted close. A closing window is already hidden, so visibility cannot tell."""

    closed = False

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802  Qt API
        super().closeEvent(event)
        self.closed = event.isAccepted()


def _main_window(runner: _FakeRunner | None = None, importer: AudioImporter | None = None) -> _RecordingWindow:
    return _RecordingWindow(
        AppState(),
        {DEFAULT_MODEL_KEY: runner or _FakeRunner()},
        ImportOrchestrator(importer or AudioImporter(), FakeScanner()),
        AppSettings(),
    )


@pytest.fixture
def window(qtbot) -> _RecordingWindow:
    w = _main_window()
    qtbot.addWidget(w)
    return w


def _welcome_paths(window: MainWindow) -> list[str]:
    recent_list = window._welcome_panel.ui.recent_list
    return [recent_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(recent_list.count())]


def test_missing_recent_project_is_kept_until_removed_from_the_toast(
    window: MainWindow, tmp_path: Path, toasts
) -> None:
    missing = str(tmp_path / "unplugged-drive" / "project")
    kept = str(tmp_path / "other-project")
    window._settings.add_recent_project(kept)
    window._settings.add_recent_project(missing)
    window._rebuild_recent_menu()

    window._open_recent(missing)

    # A drive that is merely unplugged must not cost the user the entry.
    assert window._settings.recent_projects == [missing, kept]
    [(kind, title, text, kwargs)] = toasts
    assert (kind, title) == ("warning", "Project not found")
    assert missing in text

    links = dict(kwargs["links"])
    assert list(links) == ["Locate…", "Remove from list"]
    links["Remove from list"]()
    assert window._settings.recent_projects == [kept]
    assert _welcome_paths(window) == [kept]
    menu_texts = [a.text() for a in window.ui.recent_projects_menu.actions()]
    assert paths.contract_user_path(kept) in menu_texts
    assert paths.contract_user_path(missing) not in menu_texts


def test_choosing_a_recent_project_from_the_menu_opens_it(window: MainWindow, tmp_path: Path, toasts) -> None:
    missing = str(tmp_path / "unplugged-drive" / "project")
    window._settings.add_recent_project(missing)
    window._rebuild_recent_menu()
    [entry] = [a for a in window.ui.recent_projects_menu.actions() if a.text() == paths.contract_user_path(missing)]

    entry.trigger()

    [(_kind, title, text, _kwargs)] = toasts
    assert title == "Project not found"
    assert missing in text


def test_removing_a_recent_project_on_the_welcome_screen_updates_settings_and_menu(
    window: MainWindow, tmp_path: Path
) -> None:
    first, second = str(tmp_path / "first"), str(tmp_path / "second")
    window._settings.add_recent_project(first)
    window._settings.add_recent_project(second)
    window._rebuild_recent_menu()

    window._welcome_panel.removeRecentRequested.emit(first)

    assert window._settings.recent_projects == [second]
    assert _welcome_paths(window) == [second]
    menu_texts = [a.text() for a in window.ui.recent_projects_menu.actions()]
    assert paths.contract_user_path(first) not in menu_texts


def _locate(window: MainWindow, monkeypatch, old: str, chosen: Path) -> list[Path]:
    """Run the Locate flow for *old* with the folder dialog answering *chosen*. Returns the folders opened."""
    monkeypatch.setattr(
        "pam_analyzer.ui.main_window.QFileDialog.getExistingDirectory", lambda *_a, **_k: str(chosen)
    )
    opened: list[Path] = []
    monkeypatch.setattr(window, "_load_and_remember", opened.append)
    window._welcome_panel.locateRecentRequested.emit(old)
    return opened


def test_locate_replaces_the_entry_in_place_and_opens_the_project(
    window: MainWindow, tmp_path: Path, monkeypatch
) -> None:
    first, moved, last = (str(tmp_path / n) for n in ("first", "old-location", "last"))
    for p in (last, moved, first):
        window._settings.add_recent_project(p)
    new_folder = tmp_path / "new-location"
    new_folder.mkdir()
    paths.project_toml(new_folder).touch()

    opened = _locate(window, monkeypatch, moved, new_folder)

    assert window._settings.recent_projects == [first, str(new_folder), last]
    assert _welcome_paths(window) == [first, str(new_folder), last]
    assert opened == [new_folder]


def test_locate_rejects_a_folder_without_a_project(
    window: MainWindow, tmp_path: Path, monkeypatch, toasts
) -> None:
    moved = str(tmp_path / "old-location")
    window._settings.add_recent_project(moved)

    opened = _locate(window, monkeypatch, moved, tmp_path)

    assert window._settings.recent_projects == [moved]
    assert opened == []
    [(kind, title, _text, _kwargs)] = toasts
    assert (kind, title) == ("warning", "Not a project folder")


@pytest.fixture
def confirm_stop(monkeypatch) -> list[list[str]]:
    """Answer the stop-running-tasks prompt with Quit, recording what it lists."""
    asked: list[list[str]] = []

    def _confirm(_self, busy, _action, _doing) -> bool:
        asked.append(busy)
        return True

    monkeypatch.setattr(MainWindow, "_confirm_stop", _confirm)
    return asked


def test_closing_an_idle_window_closes_at_once(window: _RecordingWindow, confirm_stop) -> None:
    assert window.close()
    assert window.closed
    assert confirm_stop == []


class _RunnerUntilCancelled(_FakeRunner):
    def __init__(self) -> None:
        self.cancelled = threading.Event()

    def run(self, *, progress, **kwargs) -> AnalysisRunResult:
        if not wait_until_cancelled(progress.is_cancelled):
            return AnalysisRunResult(status=RunStatus.COMPLETED)
        self.cancelled.set()
        return AnalysisRunResult(status=RunStatus.CANCELLED)


class _StuckRunner(_FakeRunner):
    """Ignores cancellation, like a write the runner cannot interrupt, until released."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def run(self, **kwargs) -> AnalysisRunResult:
        self.release.wait()
        return AnalysisRunResult(status=RunStatus.CANCELLED)


def _window_running_analysis(qtbot, tmp_path: Path, load_project, runner: _FakeRunner) -> _RecordingWindow:
    w = _main_window(runner)
    qtbot.addWidget(w)
    Campaign(name="alpha", folder=tmp_path / "alpha", species_filter_mode=FilterMode.LIST).create()
    Project(folder=tmp_path).save()
    load_project(w._app_state, tmp_path)
    w._birdnet_panel._start_run()
    qtbot.waitUntil(lambda: w._birdnet_panel._thread.isRunning())
    w.show()
    return w


@pytest.fixture
def stuck_analysis(qtbot, tmp_path: Path, load_project, confirm_stop):
    """A window whose analysis keeps running after the close interrupts it, until released."""
    runner = _StuckRunner()
    w = _window_running_analysis(qtbot, tmp_path, load_project, runner)
    thread = w._birdnet_panel._thread
    yield w, runner.release
    runner.release.set()
    if shiboken6.isValid(thread):  # the panel deletes it once the run ends
        thread.wait()


def test_close_hides_at_once_then_closes_when_the_analysis_finishes(stuck_analysis, qtbot) -> None:
    w, release = stuck_analysis

    assert not w.close()
    assert not w.isVisible()
    assert not w.closed

    release.set()

    qtbot.waitUntil(lambda: w.closed)
    assert not w.running_threads()


def test_close_gives_up_on_a_stuck_analysis_after_the_deadline(stuck_analysis, qtbot, monkeypatch) -> None:
    w, _release = stuck_analysis
    monkeypatch.setattr(main_window, "_CLOSE_TIMEOUT_S", 0.2)
    w.close()

    qtbot.waitUntil(lambda: w.closed)
    assert w.running_threads()


def test_closing_cancels_a_running_analysis(qtbot, tmp_path: Path, load_project, confirm_stop) -> None:
    runner = _RunnerUntilCancelled()
    w = _window_running_analysis(qtbot, tmp_path, load_project, runner)

    assert not w.close()

    qtbot.waitUntil(lambda: w.closed)
    assert runner.cancelled.is_set()
    assert confirm_stop == [[f"{DEFAULT_MODEL_KEY} analysis"]]


def test_declining_the_quit_prompt_keeps_the_analysis_running(
    qtbot, tmp_path: Path, load_project, monkeypatch
) -> None:
    w = _window_running_analysis(qtbot, tmp_path, load_project, _RunnerUntilCancelled())
    monkeypatch.setattr(MainWindow, "_confirm_stop", lambda *_a: False)

    assert not w.close()

    assert w.isVisible()
    assert w._birdnet_panel._thread.isRunning()
    w._birdnet_panel.request_shutdown()


@pytest.fixture
def stuck_load(window: MainWindow, tmp_path: Path, monkeypatch):
    """Start a project load that blocks, like a scan of a slow drive, until released."""
    release = threading.Event()

    def _stuck_load(_folder: Path) -> None:
        release.wait()
        raise OSError("drive released")

    monkeypatch.setattr(project_load_worker, "load_project", _stuck_load)
    window._start_project_load(tmp_path)
    thread = window._project_load_thread
    yield release
    release.set()
    if shiboken6.isValid(thread):  # the window deletes it once the load ends
        thread.wait()
        # Deliver the queued failure while the toasts fixture still records. pytest-qt
        # processes events again once it is undone, which would show a real toast.
        # Not qtbot.wait: its event loop runs the window's pending deleteLater first.
        QCoreApplication.processEvents()


def test_closing_does_not_wait_for_a_project_load(
    window: _RecordingWindow, stuck_load: threading.Event, confirm_stop
) -> None:
    window.show()

    assert window.close()

    assert window.running_threads()  # main() exits past it
    assert confirm_stop == []


def test_closing_does_not_wait_for_an_audio_size_pass(
    window: _RecordingWindow, qtbot, tmp_path: Path, load_project, monkeypatch, confirm_stop
) -> None:
    def _sizes_until_cancelled(inventory, is_cancelled):
        wait_until_cancelled(is_cancelled)
        return inventory

    monkeypatch.setattr(audio_inventory_refresher, "resolve_audio_sizes", _sizes_until_cancelled)
    Campaign(name="alpha", folder=tmp_path / "alpha", species_filter_mode=FilterMode.LIST).create()
    week = tmp_path / "alpha" / "MSD-X" / "week_01"
    week.mkdir(parents=True)
    (week / "20240101_120000.WAV").write_bytes(b"\x00" * 4096)
    Project(folder=tmp_path).save()
    load_project(window._app_state, tmp_path)  # starts the size pass
    window.show()
    assert window.running_threads()

    assert window.close()

    assert confirm_stop == []
    qtbot.waitUntil(lambda: not window.running_threads())  # the close still interrupted it


def test_closing_during_a_folder_import_does_not_start_the_next_card(qtbot, tmp_path: Path, confirm_stop) -> None:
    importer = ImporterUntilCancelled()
    w = _main_window(importer=importer)
    qtbot.addWidget(w)
    campaign = Campaign(name="alpha", folder=tmp_path / "alpha", species_filter_mode=FilterMode.LIST)
    campaign.create()
    w._import_orchestrator.start_folder_import(campaign, [folder_card(tmp_path, "CardA"), folder_card(tmp_path, "CardB")])
    qtbot.waitUntil(lambda: importer.imported == ["CardA"])
    w.show()

    assert not w.close()

    qtbot.waitUntil(lambda: w.closed)
    assert importer.imported == ["CardA"]


def test_deleting_a_closed_window_stops_the_spectrogram_render_thread(qtbot) -> None:
    # Not qtbot.addWidget: the window's C++ side is deleted here, so qtbot's own
    # close at teardown would hit a deleted object.
    w = _main_window()
    render_thread = w.findChild(SpectrogramWidget)._thread
    assert w.close()

    shiboken6.delete(w)

    assert not render_thread.isRunning()
