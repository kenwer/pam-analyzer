"""End-to-end pytest-qt coverage for folder import via drag-and-drop: drives
the real dragEnterEvent/dropEvent handlers and the real FolderImportDialog,
and confirms files land in campaign_folder/<card>/week_NN/ exactly like an SD
import. There is no dedicated button; dropping a folder onto the widget while
viewing a campaign is the only entry point.

FolderImportDialog's exec() is monkeypatched in every test: a real exec()
would block on a Qt event loop that nothing in an offscreen test session can
dismiss (QApplication.activeModalWidget() is not reliably populated under
QT_QPA_PLATFORM=offscreen, so a QTimer-based "click OK once the dialog
appears" approach can hang forever).
"""

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QDialog

from pam_analyzer.domain import Campaign, FilterMode, LatLon, Project
from pam_analyzer.domain.audio_import import DetectedCard
from pam_analyzer.infrastructure import (
    AudioImporter,
    TomlCampaignRepository,
    TomlProjectRepository,
)
from pam_analyzer.ui.app_state import AppState
from pam_analyzer.ui.dialogs.folder_import_dialog import FolderImportDialog
from pam_analyzer.ui.panels.campaigns_panel import CampaignsPanel
from pam_analyzer.workers import ImportOrchestrator

SR = 48_000
STAMP = "20260619_073000"  # parses to a recording time, so week bucketing is exercised


class _FakeScanner:
    """Minimal stand-in for PsutilSdCardScanner; records any eject() calls."""

    def __init__(self) -> None:
        self.ejected: list[DetectedCard] = []

    def scan(self, name_pattern: str) -> list[DetectedCard]:
        return []

    def eject(self, card: DetectedCard) -> None:
        self.ejected.append(card)


def _write_wav(path: Path) -> None:
    rng = np.random.default_rng(0)
    audio = rng.integers(-32768, 32767, size=SR // 4, dtype=np.int16)
    sf.write(path, audio, SR, subtype="PCM_16")


def _drop_event(paths: list[Path]) -> QDropEvent:
    """A QDropEvent carrying *paths* as local file URLs, for feeding straight
    into CampaignDetailWidget.dropEvent() without simulating a real mouse drag
    (real DnD simulation is flaky under pytest-qt/offscreen).

    The QMimeData is stashed on the event as _mime: QDropEvent doesn't take
    Python ownership of it, so without a kept reference it gets garbage
    collected as soon as this function returns, and event.mimeData() then
    dereferences a dangling pointer (segfault) the moment dropEvent() calls it.
    """
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    event = QDropEvent(
        QPointF(0, 0),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    event._mime = mime  # type: ignore[attr-defined]
    return event


@pytest.fixture(autouse=True)
def _isolated_qsettings(tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QSettings

    QCoreApplication.setOrganizationName("PAMAnalyzerTest")
    QCoreApplication.setApplicationName(f"PAMAnalyzerTest-{tmp_path.name}")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "qsettings"))
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "qsettings")
    )
    yield


@pytest.fixture
def project_with_campaign(tmp_path: Path) -> tuple[Project, Campaign]:
    audio_root = tmp_path / "audio"
    audio_root.mkdir()
    campaign = Campaign(
        name="alpha",
        folder=audio_root / "alpha",
        species_filter_mode=FilterMode.LOCATION,
        location=LatLon(48.0, 11.0),
    )
    TomlCampaignRepository().create(campaign)
    proj = Project(path=tmp_path / "demo.pamproj", audio_recordings_path=audio_root)
    TomlProjectRepository().save(proj)
    return proj, campaign


@pytest.fixture
def scanner() -> _FakeScanner:
    return _FakeScanner()


@pytest.fixture
def panel(qtbot, project_with_campaign, scanner: _FakeScanner) -> CampaignsPanel:
    proj, _ = project_with_campaign
    state = AppState(TomlProjectRepository(), TomlCampaignRepository())
    orchestrator = ImportOrchestrator(AudioImporter(), scanner)
    p = CampaignsPanel(state, TomlCampaignRepository(), orchestrator)
    qtbot.addWidget(p)
    state.load_project(proj.path)
    return p


def _open_view_page(qtbot, panel: CampaignsPanel) -> None:
    index = panel._model.index(0, 0)
    panel.ui.campaign_list.setCurrentIndex(index)
    qtbot.waitUntil(
        lambda: panel._detail.ui.stack.currentWidget() is panel._detail.ui.view_page, timeout=1000
    )


def test_import_hint_mentions_drag_and_drop(qtbot, panel: CampaignsPanel):
    _open_view_page(qtbot, panel)
    detail = panel._detail
    assert "drag a folder" in detail.ui.import_hint_label.text()


def test_single_card_folder_import_writes_week_folder(
    qtbot, panel: CampaignsPanel, project_with_campaign, scanner: _FakeScanner, tmp_path, monkeypatch
):
    _, campaign = project_with_campaign
    detail = panel._detail
    _open_view_page(qtbot, panel)

    source = tmp_path / "OldRecordings"
    source.mkdir()
    _write_wav(source / f"{STAMP}.WAV")

    # Auto-accept the confirmation dialog without editing names or opening a
    # real modal event loop (see module docstring for why).
    monkeypatch.setattr(FolderImportDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    detail.dropEvent(_drop_event([source]))

    qtbot.waitUntil(lambda: not detail._is_folder_importing, timeout=5000)

    dest = campaign.folder / "OldRecordings" / "week_23"  # birdnet_week(2026-06-19) == 23
    qtbot.waitUntil(lambda: dest.exists() and any(dest.iterdir()), timeout=5000)
    assert any(f.suffix == ".flac" for f in dest.iterdir())
    # The safety fix under test: a folder-sourced import must never eject.
    assert scanner.ejected == []


def test_batch_subfolder_import_creates_one_card_per_subfolder(
    qtbot, panel: CampaignsPanel, project_with_campaign, scanner: _FakeScanner, tmp_path, monkeypatch
):
    """Dropping a folder with no audio at its root but two subfolders that each
    hold audio should import each subfolder as its own card (discover_folder_cards'
    batch branch), landing as two independent card folders under the campaign.
    """
    _, campaign = project_with_campaign
    detail = panel._detail
    _open_view_page(qtbot, panel)

    source = tmp_path / "OffloadedCards"
    source.mkdir()
    card_a = source / "CardA"
    card_a.mkdir()
    _write_wav(card_a / f"{STAMP}.WAV")
    card_b = source / "CardB"
    card_b.mkdir()
    _write_wav(card_b / f"{STAMP}.WAV")

    def fake_exec(self) -> QDialog.DialogCode:
        # Two rows are expected (one per subfolder); leave names as proposed.
        assert self.ui.card_table.rowCount() == 2
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(FolderImportDialog, "exec", fake_exec)
    detail.dropEvent(_drop_event([source]))

    qtbot.waitUntil(lambda: not detail._is_folder_importing, timeout=5000)

    for name in ("CardA", "CardB"):
        dest = campaign.folder / name / "week_23"
        qtbot.waitUntil(lambda dest=dest: dest.exists() and any(dest.iterdir()), timeout=5000)
        assert any(f.suffix == ".flac" for f in dest.iterdir())
    assert scanner.ejected == []


def test_dropping_multiple_folders_imports_each_as_its_own_card(
    qtbot, panel: CampaignsPanel, project_with_campaign, scanner: _FakeScanner, tmp_path, monkeypatch
):
    """Dropping two separate top-level folders at once (as opposed to one
    folder containing subfolders) must import each as its own card too --
    discover_folder_cards runs once per dropped root and the results
    concatenate, so this needs no dedicated batching logic of its own."""
    _, campaign = project_with_campaign
    detail = panel._detail
    _open_view_page(qtbot, panel)

    card_a = tmp_path / "MSD-11111"
    card_a.mkdir()
    _write_wav(card_a / f"{STAMP}.WAV")
    card_b = tmp_path / "MSD-22222"
    card_b.mkdir()
    _write_wav(card_b / f"{STAMP}.WAV")

    def fake_exec(self) -> QDialog.DialogCode:
        assert self.ui.card_table.rowCount() == 2
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(FolderImportDialog, "exec", fake_exec)
    detail.dropEvent(_drop_event([card_a, card_b]))

    qtbot.waitUntil(lambda: not detail._is_folder_importing, timeout=5000)

    for name in ("MSD-11111", "MSD-22222"):
        dest = campaign.folder / name / "week_23"
        qtbot.waitUntil(lambda dest=dest: dest.exists() and any(dest.iterdir()), timeout=5000)
    assert scanner.ejected == []


def test_folder_import_deletes_source_when_clear_after_checked(
    qtbot, panel: CampaignsPanel, project_with_campaign, scanner: _FakeScanner, tmp_path, monkeypatch
):
    """clear_after is a checkbox inside FolderImportDialog itself (scoped to
    the batch being confirmed), not the SD-only clear_check next to
    watch_button, since 'clear card after copy' reads oddly for a folder."""
    _, campaign = project_with_campaign
    detail = panel._detail
    _open_view_page(qtbot, panel)

    source = tmp_path / "OldRecordings"
    source.mkdir()
    src_file = source / f"{STAMP}.WAV"
    _write_wav(src_file)

    def fake_exec(self) -> QDialog.DialogCode:
        self.ui.clear_after_check.setChecked(True)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(FolderImportDialog, "exec", fake_exec)
    detail.dropEvent(_drop_event([source]))

    qtbot.waitUntil(lambda: not detail._is_folder_importing, timeout=5000)
    qtbot.waitUntil(lambda: not src_file.exists(), timeout=5000)


def test_watch_button_becomes_cancel_during_folder_import(
    qtbot, panel: CampaignsPanel, tmp_path, monkeypatch
):
    """There is no dedicated folder-import button, so watch_button doubles as
    its cancel control: it relabels to 'Cancel Folder Import' and stays
    enabled (rather than being disabled, as it is while an SD watch runs)."""
    detail = panel._detail
    _open_view_page(qtbot, panel)

    source = tmp_path / "OldRecordings"
    source.mkdir()
    _write_wav(source / f"{STAMP}.WAV")

    monkeypatch.setattr(FolderImportDialog, "exec", lambda self: QDialog.DialogCode.Accepted)

    assert detail.ui.watch_button.text() == "Start SD import"
    assert detail.ui.watch_button.isEnabled()

    detail.dropEvent(_drop_event([source]))
    # start_folder_import() emits folder_import_started synchronously, inside
    # the same call stack as dropEvent(), before control returns here -- so
    # by the time dropEvent() returns, the button must already read Cancel.
    assert detail.ui.watch_button.text() == "Cancel Folder Import"
    assert detail.ui.watch_button.isEnabled()

    detail.ui.watch_button.click()
    qtbot.waitUntil(lambda: not detail._is_folder_importing, timeout=5000)
    assert detail.ui.watch_button.text() == "Start SD import"
