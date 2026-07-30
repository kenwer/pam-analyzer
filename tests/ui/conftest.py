"""Shared fixtures for tests/ui.

AppState has one entry point for a loaded project: apply_loaded_project().
There is no synchronous load_project() shortcut, so opening a project in a
test drives the same ProjectLoadWorker/QThread pair MainWindow does, rather
than a second, harness-only path.
"""

from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThread

from pam_analyzer.ui.app_state import AppState
from pam_analyzer.workers import ProjectLoadWorker


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
