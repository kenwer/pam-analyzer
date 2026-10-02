"""Shared fixtures for tests/ui.

AppState has one entry point for a loaded project: apply_loaded_project().
There is no synchronous load_project() shortcut, so opening a project in a
test drives the same ProjectLoadWorker/QThread pair MainWindow does, rather
than a second, harness-only path.
"""

from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QSettings, Qt, QThread

from pam_analyzer.ui import toasts as toasts_module
from pam_analyzer.ui.app_state import AppState
from pam_analyzer.ui.settings import AppSettings
from pam_analyzer.workers import ProjectLoadWorker


@pytest.fixture(autouse=True)
def _isolated_qsettings(tmp_path, monkeypatch) -> None:
    """Route QSettings to a per-test scratch directory so AppSettings reads
    don't leak between tests or pollute the developer's real config."""
    QCoreApplication.setOrganizationName("PAMAnalyzerTest")
    QCoreApplication.setApplicationName(f"PAMAnalyzerTest-{tmp_path.name}")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "qsettings"))
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(tmp_path / "qsettings"),
    )
    # AppSettings uses the QSettings(organization, application) constructor,
    # which Qt hardcodes to NativeFormat (the real CFPreferences store on
    # macOS) regardless of setDefaultFormat()/setPath() above. Redirect it
    # separately via an explicit file-backed QSettings so tests can never
    # write to the developer's actual application preferences.
    ini_path = tmp_path / "qsettings" / "app_settings.ini"
    monkeypatch.setattr(
        AppSettings,
        "__init__",
        lambda self: setattr(self, "_settings", QSettings(str(ini_path), QSettings.Format.IniFormat)),
    )


@pytest.fixture(autouse=True)
def toasts(monkeypatch) -> list[tuple[str, str, str, dict]]:
    """Record (kind, title, text, kwargs) per toast instead of showing one.

    kind is the preset name in lower case ("success", "warning", ...). A real
    pyqttoast keeps class-level queues and timers alive past the test.
    """
    shown: list[tuple[str, str, str, dict]] = []

    def record(_parent, title, text, preset, **kwargs):
        shown.append((preset.name.lower(), title, text, kwargs))

    monkeypatch.setattr(toasts_module, "_show_toast", record)
    return shown


@pytest.fixture
def load_project(qtbot):
    """Load a project folder through a real ProjectLoadWorker and apply the
    result to state, the same as MainWindow's succeeded handler.

    Returns a callable, load_project(state, folder), so a test body reads
    like the old state.load_project(folder) it replaces.
    """

    def _load(state: AppState, folder: Path) -> None:
        thread = QThread()
        worker = ProjectLoadWorker(folder)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)

        outcome: dict[str, object] = {}
        worker.succeeded.connect(lambda result: outcome.__setitem__("result", result))
        worker.failed.connect(lambda message: outcome.__setitem__("error", message))
        # DirectConnection: quit() runs inline on the worker thread as run()
        # returns, so exec() (entered right after) sees the quit request
        # immediately instead of blocking on the test's event loop.
        worker.succeeded.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        worker.failed.connect(thread.quit, Qt.ConnectionType.DirectConnection)

        thread.start()
        qtbot.waitUntil(lambda: not thread.isRunning(), timeout=5000)
        thread.wait()
        worker.deleteLater()
        thread.deleteLater()

        if "error" in outcome:
            raise RuntimeError(outcome["error"])
        result = outcome["result"]
        state.apply_loaded_project(
            result.project, result.campaigns, result.audio_inventory, result.analysis_inventory
        )

    return _load
