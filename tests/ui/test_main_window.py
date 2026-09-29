"""pytest-qt tests for MainWindow behaviour that spans several panels."""

from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from pam_analyzer.domain import AnalysisRunResult, RunStatus, paths
from pam_analyzer.infrastructure import AudioImporter
from pam_analyzer.ui.app_state import AppState
from pam_analyzer.ui.main_window import MainWindow
from pam_analyzer.ui.settings import AppSettings
from pam_analyzer.workers import ImportOrchestrator
from tests.conftest import DEFAULT_MODEL_KEY


class _FakeRunner:
    model_key = DEFAULT_MODEL_KEY

    def count_audio_files(self, _path: Path) -> int:
        return 0

    def available_locales(self) -> list[str]:
        return ["en"]

    def run(self, **kwargs) -> AnalysisRunResult:
        return AnalysisRunResult(status=RunStatus.COMPLETED)


class _FakeScanner:
    def scan(self, name_pattern: str) -> list:
        return []

    def eject(self, card) -> None:  # pragma: no cover - test helper
        pass


@pytest.fixture(autouse=True)
def _isolated_qsettings(tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QSettings

    QCoreApplication.setOrganizationName("PAMAnalyzerTest")
    QCoreApplication.setApplicationName(f"PAMAnalyzerTest-{tmp_path.name}")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "qsettings"))
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(tmp_path / "qsettings"),
    )
    # AppSettings' QSettings(organization, application) constructor ignores the
    # redirects above on macOS, so point it at an explicit ini file instead.
    ini_path = tmp_path / "qsettings" / "app_settings.ini"
    monkeypatch.setattr(
        AppSettings,
        "__init__",
        lambda self: setattr(self, "_settings", QSettings(str(ini_path), QSettings.Format.IniFormat)),
    )
    yield


@pytest.fixture
def window(qtbot) -> MainWindow:
    w = MainWindow(
        AppState(),
        {DEFAULT_MODEL_KEY: _FakeRunner()},
        ImportOrchestrator(AudioImporter(), _FakeScanner()),
        AppSettings(),
    )
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
