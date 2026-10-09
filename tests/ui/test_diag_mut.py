"""Temporary mutation variants of the Windows crash, picked by the DIAG env var.

A copy of the smallest crashing shape: the folder import file's panel fixture
plus two tests that use it. The crash comes in the second test's fixture setup.

    base       unchanged copy, has to crash for the other variants to mean anything
    immortal   panels are never handed to qtbot and never released, so nothing
               from the first test is destroyed while the second one loads
    nomap      the map picker's QQuickWidget never loads its QML, so there is no
               QtLocation map, no tile fetching and no scene graph
    deadproxy  the map loads, but every network request goes to a closed local
               port and fails before any TLS handshake
    schannel   the map loads and fetches, with Qt's TLS pinned to the Windows
               backend instead of OpenSSL

Round 5 showed the crash comes when the first test's panel is destroyed (the
preflush variant died inside the flush itself, with no loader thread alive),
and always on a native thread that has no Python state. Every panel holds a
live OSM map that fetches tiles over HTTPS, hence the variants above.

With DIAG_INFO set, the second test's fixture also writes the loaded modules
and Qt's TLS backend details to diag-info-<DIAG_INFO>.txt before it loads.

Must not reach master.
"""

import os
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThread
from PySide6.QtNetwork import QNetworkProxy, QSslSocket
from PySide6.QtQuickWidgets import QQuickWidget

from pam_analyzer.domain import Campaign, FilterMode, LatLon, Project
from pam_analyzer.infrastructure import AudioImporter
from pam_analyzer.ui.app_state import AppState
from pam_analyzer.ui.panels.campaigns_panel import CampaignsPanel
from pam_analyzer.ui.settings import AppSettings
from pam_analyzer.workers import ImportOrchestrator, ProjectLoadWorker

from .test_campaign_detail_widget_folder_import import _FakeScanner, _open_view_page

DIAG = os.environ.get("DIAG", "base")
DIAG_INFO = os.environ.get("DIAG_INFO", "")
_immortals: list[object] = []
_panels_built = 0

if DIAG == "schannel":
    QSslSocket.setActiveBackend("schannel")


def _write_info() -> None:
    """Loaded modules first, then the TLS details, since asking for those can
    itself load a backend."""
    import psutil

    # memory_maps exists on Windows and Linux but not on macOS.
    try:
        modules = sorted({m.path for m in psutil.Process().memory_maps()})
    except AttributeError:
        modules = ["(memory_maps unavailable on this platform)"]
    lines = [f"module {m}" for m in modules]
    lines += [
        f"tls available {QSslSocket.availableBackends()}",
        f"tls active {QSslSocket.activeBackend()}",
        f"tls supportsSsl {QSslSocket.supportsSsl()}",
        f"tls runtime {QSslSocket.sslLibraryVersionString()}",
        f"tls built against {QSslSocket.sslLibraryBuildVersionString()}",
    ]
    Path(f"diag-info-{DIAG_INFO}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load(qtbot, state: AppState, folder: Path) -> None:
    """tests/ui/conftest.py load_project, copied so a variant can alter it."""
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
def panel(qtbot, tmp_path: Path, monkeypatch) -> CampaignsPanel:
    global _panels_built
    _panels_built += 1
    if DIAG_INFO and _panels_built == 2:
        _write_info()
    if DIAG == "nomap":
        monkeypatch.setattr(QQuickWidget, "setSource", lambda self, url: None)
    elif DIAG == "deadproxy":
        QNetworkProxy.setApplicationProxy(QNetworkProxy(QNetworkProxy.ProxyType.HttpProxy, "127.0.0.1", 9))

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
    _open_view_page(qtbot, panel)
    assert "drag a folder" in panel._detail.ui.import_hint_label.text()


def test_b(qtbot, panel: CampaignsPanel):
    assert panel is not None


def test_c(qtbot, panel: CampaignsPanel):
    assert panel is not None
