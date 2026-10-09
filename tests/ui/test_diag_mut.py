"""Temporary mutation variants of the Windows crash, picked by the DIAG env var.

A copy of the smallest crashing shape: the folder import file's panel fixture
plus two tests that use it. The crash comes in the second test's fixture setup.

    base      unchanged copy, has to crash for the other variants to mean anything
    noview    the first test does not open the campaign's view page
    preflush  run pending deferred deletes on the main thread before the loader
              thread starts
    immortal  panels are never handed to qtbot and never released, so nothing
              from the first test is destroyed while the second one loads

Must not reach master.
"""

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QThread

from pam_analyzer.domain import Campaign, FilterMode, LatLon, Project
from pam_analyzer.infrastructure import AudioImporter
from pam_analyzer.ui.app_state import AppState
from pam_analyzer.ui.panels.campaigns_panel import CampaignsPanel
from pam_analyzer.ui.settings import AppSettings
from pam_analyzer.workers import ImportOrchestrator, ProjectLoadWorker

from .test_campaign_detail_widget_folder_import import _FakeScanner, _open_view_page

DIAG = os.environ.get("DIAG", "base")
_immortals: list[object] = []


def _load(qtbot, state: AppState, folder: Path) -> None:
    """tests/ui/conftest.py load_project, copied so a variant can alter it."""
    if DIAG == "preflush":
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()

    thread = QThread()
    worker = ProjectLoadWorker(folder)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)

    outcome: dict[str, object] = {}
    worker.succeeded.connect(lambda result: outcome.__setitem__("result", result))
    worker.failed.connect(lambda message: outcome.__setitem__("error", message))
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


@pytest.fixture
def panel(qtbot, tmp_path: Path) -> CampaignsPanel:
    folder = tmp_path / "proj"
    folder.mkdir()
    Campaign(
        name="alpha",
        folder=folder / "alpha",
        species_filter_mode=FilterMode.LOCATION,
        location=LatLon(48.0, 11.0),
    ).create()
    Project(folder=folder).save()

    state = AppState()
    orchestrator = ImportOrchestrator(AudioImporter(), _FakeScanner())
    p = CampaignsPanel(state, orchestrator, AppSettings())
    if DIAG == "immortal":
        _immortals.extend((p, state, orchestrator))
    else:
        qtbot.addWidget(p)
    _load(qtbot, state, folder)
    return p


def test_a(qtbot, panel: CampaignsPanel):
    if DIAG != "noview":
        _open_view_page(qtbot, panel)
        assert "drag a folder" in panel._detail.ui.import_hint_label.text()


def test_b(qtbot, panel: CampaignsPanel):
    assert panel is not None


def test_c(qtbot, panel: CampaignsPanel):
    assert panel is not None
