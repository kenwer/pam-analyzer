"""Temporary bisection of the Windows crash in the folder import panel fixture.

Each test name is one rung of a ladder from "only the loader thread" up to
"the full fixture plus opening the view page". The diagnosis workflow runs
each rung in its own pytest process, five times over, so the lowest rung that
crashes names the ingredient. Must not reach master.
"""

from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThread

from pam_analyzer.domain import Campaign, FilterMode, LatLon, Project
from pam_analyzer.infrastructure import AudioImporter
from pam_analyzer.ui.app_state import AppState
from pam_analyzer.ui.panels.campaigns_panel import CampaignsPanel
from pam_analyzer.ui.settings import AppSettings
from pam_analyzer.workers import ImportOrchestrator, ProjectLoadWorker

from .test_campaign_detail_widget_folder_import import _FakeScanner, _open_view_page

REPEAT = range(5)


@pytest.fixture
def project_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "proj"
    folder.mkdir()
    Campaign(
        name="alpha",
        folder=folder / "alpha",
        species_filter_mode=FilterMode.LOCATION,
        location=LatLon(48.0, 11.0),
    ).create()
    Project(folder=folder).save()
    return folder


def _make_panel(qtbot, load_project, folder: Path) -> CampaignsPanel:
    state = AppState()
    panel = CampaignsPanel(state, ImportOrchestrator(AudioImporter(), _FakeScanner()), AppSettings())
    qtbot.addWidget(panel)
    load_project(state, folder)
    return panel


@pytest.mark.parametrize("i", REPEAT)
def test_rung1_loader_only(qtbot, project_folder: Path, i: int):
    """The conftest load_project thread dance, with the result thrown away."""
    thread = QThread()
    worker = ProjectLoadWorker(project_folder)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    outcome: list[object] = []
    worker.succeeded.connect(outcome.append)
    worker.succeeded.connect(thread.quit, Qt.ConnectionType.DirectConnection)
    worker.failed.connect(thread.quit, Qt.ConnectionType.DirectConnection)
    thread.start()
    qtbot.waitUntil(lambda: not thread.isRunning(), timeout=5000)
    thread.wait()
    worker.deleteLater()
    thread.deleteLater()
    assert outcome


@pytest.mark.parametrize("i", REPEAT)
def test_rung2_state_only(qtbot, project_folder: Path, load_project, i: int):
    """Adds AppState, whose apply_loaded_project starts the size refresher thread."""
    load_project(AppState(), project_folder)


@pytest.mark.parametrize("i", REPEAT)
def test_rung3_panel(qtbot, project_folder: Path, load_project, i: int):
    """Adds the CampaignsPanel widget tree. This is the crashing fixture."""
    _make_panel(qtbot, load_project, project_folder)


@pytest.mark.parametrize("i", REPEAT)
def test_rung4_panel_view(qtbot, project_folder: Path, load_project, i: int):
    """Adds what the first folder import test does: open the campaign's view page."""
    _open_view_page(qtbot, _make_panel(qtbot, load_project, project_folder))
