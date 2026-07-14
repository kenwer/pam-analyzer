"""pytest-qt smoke tests for the Campaigns panel."""

import os
import time
from pathlib import Path

import pytest

from pam_analyzer.domain import Campaign, FilterMode, LatLon, Project
from pam_analyzer.domain.audio_import import DetectedCard, ImportSource
from pam_analyzer.infrastructure import (
    AudioImporter,
    TomlCampaignRepository,
    TomlProjectRepository,
)
from pam_analyzer.ui.app_state import AppState
from pam_analyzer.ui.panels.campaigns_panel import (
    SORT_ORDER_LABELS,
    CampaignSortOrder,
    CampaignsPanel,
)
from pam_analyzer.ui.settings import AppSettings
from pam_analyzer.workers import ImportOrchestrator


class _FakeScanner:
    """Minimal stand-in for PsutilSdCardScanner used by CampaignDetailWidget."""

    def __init__(self) -> None:
        self._cards: list[DetectedCard] = []

    def set_cards(self, cards: list[DetectedCard]) -> None:
        self._cards = list(cards)

    def scan(self, name_pattern: str) -> list[DetectedCard]:
        return list(self._cards)

    def eject(self, card: DetectedCard) -> None:  # pragma: no cover - test helper
        pass


@pytest.fixture(autouse=True)
def _isolated_qsettings(tmp_path, monkeypatch):
    """Route QSettings to a per-test scratch directory so AppSettings reads
    don't leak between tests or pollute the developer's real config."""
    from PySide6.QtCore import QCoreApplication, QSettings

    from pam_analyzer.ui.settings import AppSettings

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
def state(project_with_campaign) -> AppState:
    return AppState(TomlProjectRepository(), TomlCampaignRepository())


@pytest.fixture
def scanner() -> _FakeScanner:
    return _FakeScanner()


@pytest.fixture
def panel(
    qtbot, state: AppState, project_with_campaign, scanner: _FakeScanner
) -> CampaignsPanel:
    proj, _ = project_with_campaign
    orchestrator = ImportOrchestrator(AudioImporter(), scanner)
    p = CampaignsPanel(state, TomlCampaignRepository(), orchestrator, AppSettings())
    qtbot.addWidget(p)
    state.load_project(proj.path)
    return p


def test_panel_shows_empty_on_no_project(qtbot):
    state = AppState(TomlProjectRepository(), TomlCampaignRepository())
    orchestrator = ImportOrchestrator(AudioImporter(), _FakeScanner())
    p = CampaignsPanel(state, TomlCampaignRepository(), orchestrator, AppSettings())
    qtbot.addWidget(p)
    assert p._detail.ui.stack.currentWidget() is p._detail.ui.empty_page


def test_panel_populates_list_on_project_load(panel: CampaignsPanel):
    assert panel._model.rowCount() == 1
    assert panel._model.item(0).text() == "alpha"


def test_selecting_campaign_opens_view_page(qtbot, panel: CampaignsPanel):
    """Default landing after selecting a campaign is the view page, not the form."""
    index = panel._model.index(0, 0)
    panel.ui.campaign_list.setCurrentIndex(index)
    qtbot.waitUntil(
        lambda: panel._detail.ui.stack.currentWidget() is panel._detail.ui.view_page,
        timeout=1000,
    )
    assert panel._detail.ui.view_name_label.text() == "alpha"
    # Filter summary should describe the location-mode campaign.
    assert "Location" in panel._detail.ui.view_filter_label.text()


def test_edit_button_switches_to_form(qtbot, panel: CampaignsPanel):
    index = panel._model.index(0, 0)
    panel.ui.campaign_list.setCurrentIndex(index)
    qtbot.waitUntil(
        lambda: panel._detail.ui.stack.currentWidget() is panel._detail.ui.view_page,
        timeout=1000,
    )
    panel._detail.ui.view_edit_button.click()
    assert panel._detail.ui.stack.currentWidget() is panel._detail.ui.form_page
    assert panel._detail.ui.name_edit.text() == "alpha"


def test_form_cancel_returns_to_view_when_editing(qtbot, panel: CampaignsPanel):
    index = panel._model.index(0, 0)
    panel.ui.campaign_list.setCurrentIndex(index)
    qtbot.waitUntil(
        lambda: panel._detail.ui.stack.currentWidget() is panel._detail.ui.view_page,
        timeout=1000,
    )
    panel._detail.ui.view_edit_button.click()
    panel._detail.ui.cancel_button.click()
    assert panel._detail.ui.stack.currentWidget() is panel._detail.ui.view_page


def test_inventory_tree_reflects_imported_files(
    qtbot, panel: CampaignsPanel, state: AppState, project_with_campaign
):
    """The inventory tree on the view page should populate from disk content."""
    _proj, campaign = project_with_campaign

    # Drop some files into the campaign folder, then ask AppState to rescan.
    card = campaign.folder / "MSD-TEST"
    (card / "week_01").mkdir(parents=True)
    (card / "week_01" / "20240101_120000.WAV").write_bytes(b"\x00" * 2048)
    (card / "week_01" / "20240102_120000.WAV").write_bytes(b"\x00" * 1024)
    state.refresh_audio_inventory()

    # Select the campaign so the detail lands on the view page.
    index = panel._model.index(0, 0)
    panel.ui.campaign_list.setCurrentIndex(index)
    qtbot.waitUntil(
        lambda: panel._detail.ui.stack.currentWidget() is panel._detail.ui.view_page,
        timeout=1000,
    )

    model = panel._detail._inventory_model
    assert model.rowCount() == 1  # one card
    card_item = model.item(0, 0)
    assert card_item.text() == "MSD-TEST"
    # One week under the card; under that week, two files.
    assert card_item.rowCount() == 1
    assert card_item.child(0, 0).text() == "Week 01"
    assert card_item.child(0, 0).rowCount() == 2

    # Headline label mentions file count and ARU count.
    text = panel._detail.ui.inventory_label.text()
    assert "2" in text
    assert "ARU" in text


def test_watch_button_lives_on_view_page(qtbot, panel: CampaignsPanel):
    """After step 3 the import controls moved into the campaign view."""
    index = panel._model.index(0, 0)
    panel.ui.campaign_list.setCurrentIndex(index)
    qtbot.waitUntil(
        lambda: panel._detail.ui.stack.currentWidget() is panel._detail.ui.view_page,
        timeout=1000,
    )
    # _watch_button lives on the view page; isEnabled is the right check
    # because isVisible is False until the parent widget is show()n.
    assert panel._detail.ui.watch_button.isEnabled()
    assert panel._detail.is_busy() is False
    assert panel.is_busy() is False


def test_starting_watch_emits_importStarted(qtbot, panel: CampaignsPanel, state: AppState):
    """importStarted should fire so MainWindow can show a persistent status."""
    index = panel._model.index(0, 0)
    panel.ui.campaign_list.setCurrentIndex(index)
    qtbot.waitUntil(
        lambda: panel._detail.ui.stack.currentWidget() is panel._detail.ui.view_page,
        timeout=1000,
    )
    with qtbot.waitSignal(state.importStarted, timeout=1000) as blocker:
        panel._detail.ui.watch_button.click()
    assert blocker.args == ["alpha", ImportSource.SD_CARD]
    assert panel.is_busy() is True
    assert panel.busy_label() == "SD-card watcher"

    # Cleanup so the poll timer doesn't keep firing into other tests.
    panel._detail.request_shutdown()


def test_queue_label_shows_pending_cards(
    qtbot, panel: CampaignsPanel, scanner: _FakeScanner, project_with_campaign, tmp_path: Path
):
    """A multi-slot reader (cards inserted at once) should surface the queue."""
    proj, campaign = project_with_campaign

    # Prepare three card folders so the scanner can offer them all in one scan.
    cards = []
    for name in ("MSD-A", "MSD-B", "MSD-C"):
        card_dir = tmp_path / name
        card_dir.mkdir()
        (card_dir / "20240101_120000.WAV").write_bytes(b"\x00" * 16)
        cards.append(DetectedCard(name=name, mountpoint=card_dir, device=f"/dev/{name}"))
    scanner.set_cards(cards)

    # Land on the view page for 'alpha' and start watching.
    index = panel._model.index(0, 0)
    panel.ui.campaign_list.setCurrentIndex(index)
    qtbot.waitUntil(
        lambda: panel._detail.ui.stack.currentWidget() is panel._detail.ui.view_page,
        timeout=1000,
    )
    panel._detail.ui.watch_button.click()

    # Drive one poll synchronously; the first card pops and starts copying,
    # leaving two behind in the queue.
    panel._detail._orchestrator._poll_timer.stop()
    panel._detail._orchestrator._on_poll()

    qtbot.waitUntil(lambda: bool(panel._detail._orchestrator._queue.pending), timeout=1000)
    text = panel._detail.ui.queue_label.text()
    assert "2 cards queued" in text
    # Both pending card names should appear in the label.
    assert "MSD-B" in text
    assert "MSD-C" in text

    panel._detail.request_shutdown()


def test_campaign_switch_while_watching_prompts(
    qtbot, panel: CampaignsPanel, project_with_campaign, monkeypatch
):
    """Selecting a different campaign while a watch is active asks the user."""
    # Add a second campaign so a switch is possible.
    proj, _ = project_with_campaign
    second = Campaign(
        name="beta",
        folder=proj.audio_recordings_path / "beta",
        species_filter_mode=FilterMode.LOCATION,
        location=LatLon(50.0, 8.0),
    )
    TomlCampaignRepository().create(second)
    panel._app_state.refresh_campaigns()

    # Select 'alpha' and start watching.
    alpha_idx = next(
        panel._model.index(r, 0)
        for r in range(panel._model.rowCount())
        if panel._model.item(r).text() == "alpha"
    )
    panel.ui.campaign_list.setCurrentIndex(alpha_idx)
    qtbot.waitUntil(
        lambda: panel._detail.ui.stack.currentWidget() is panel._detail.ui.view_page,
        timeout=1000,
    )
    panel._detail.ui.watch_button.click()
    assert panel.is_busy()

    # User declines to switch -> selection should revert to alpha.
    monkeypatch.setattr(panel, "_confirm_stop_watching", lambda: False)
    beta_idx = next(
        panel._model.index(r, 0)
        for r in range(panel._model.rowCount())
        if panel._model.item(r).text() == "beta"
    )
    panel.ui.campaign_list.setCurrentIndex(beta_idx)
    current_name = panel._model.itemFromIndex(panel.ui.campaign_list.currentIndex()).text()
    assert current_name == "alpha"
    assert panel.is_busy()

    # Now accept the switch -> selection moves, watch stops.
    monkeypatch.setattr(panel, "_confirm_stop_watching", lambda: True)
    panel.ui.campaign_list.setCurrentIndex(beta_idx)
    current_name = panel._model.itemFromIndex(panel.ui.campaign_list.currentIndex()).text()
    assert current_name == "beta"
    assert not panel.is_busy()


def _add_campaign(proj: Project, name: str) -> Campaign:
    campaign = Campaign(
        name=name,
        folder=proj.audio_recordings_path / name,
        species_filter_mode=FilterMode.LOCATION,
        location=LatLon(50.0, 8.0),
    )
    TomlCampaignRepository().create(campaign)
    return campaign


def test_default_sort_order_is_name_ascending(panel: CampaignsPanel, project_with_campaign):
    """A fresh AppSettings store (no persisted choice) should default to A-Z, not mtime."""
    proj, _ = project_with_campaign
    _add_campaign(proj, "zulu")
    panel._app_state.refresh_campaigns()
    names = [panel._model.item(r).text() for r in range(panel._model.rowCount())]
    assert names == ["alpha", "zulu"]


def test_sort_by_name_descending(panel: CampaignsPanel, project_with_campaign):
    proj, _ = project_with_campaign
    _add_campaign(proj, "zulu")
    panel._app_state.refresh_campaigns()

    panel._set_sort_order(CampaignSortOrder.NAME_DESC)
    names = [panel._model.item(r).text() for r in range(panel._model.rowCount())]
    assert names == ["zulu", "alpha"]


def test_sort_by_date_modified(panel: CampaignsPanel, project_with_campaign):
    """Date-modified order reflects folder mtime, independent of the name sort."""
    proj, alpha = project_with_campaign
    zulu = _add_campaign(proj, "zulu")

    # Give 'zulu' an older mtime than 'alpha' so it sorts last despite its name.
    now = time.time()
    os.utime(alpha.folder, (now, now))
    os.utime(zulu.folder, (now - 100, now - 100))
    panel._app_state.refresh_campaigns()

    panel._set_sort_order(CampaignSortOrder.DATE_MODIFIED_DESC)
    names = [panel._model.item(r).text() for r in range(panel._model.rowCount())]
    assert names == ["alpha", "zulu"]

    panel._set_sort_order(CampaignSortOrder.DATE_MODIFIED_ASC)
    names = [panel._model.item(r).text() for r in range(panel._model.rowCount())]
    assert names == ["zulu", "alpha"]


def test_sort_order_persists_via_settings(panel: CampaignsPanel):
    panel._set_sort_order(CampaignSortOrder.NAME_DESC)
    # A fresh AppSettings instance reads the same on-disk QSettings store, so
    # the choice should survive a restart without needing a live panel.
    assert AppSettings().campaign_sort_order == CampaignSortOrder.NAME_DESC.value


def test_sort_order_preserves_selection(panel: CampaignsPanel, project_with_campaign):
    proj, _ = project_with_campaign
    _add_campaign(proj, "zulu")
    panel._app_state.refresh_campaigns()
    panel._select_by_name("zulu")

    panel._set_sort_order(CampaignSortOrder.NAME_DESC)

    current = panel._model.itemFromIndex(panel.ui.campaign_list.currentIndex())
    assert current is not None
    assert current.text() == "zulu"


def test_sort_menu_marks_current_order_checked(panel: CampaignsPanel):
    from PySide6.QtWidgets import QMenu

    parent_menu = QMenu()
    submenu = panel._build_sort_menu(parent_menu)
    checked = [a.text() for a in submenu.actions() if a.isChecked()]
    assert checked == [SORT_ORDER_LABELS[CampaignSortOrder.NAME_ASC]]


def test_context_menu_offers_new_campaign_without_selection(panel: CampaignsPanel, monkeypatch):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QMenu

    captured = {}

    def fake_exec(self, *args, **kwargs):
        captured["actions"] = [a.text() for a in self.actions()]
        return None

    monkeypatch.setattr(QMenu, "exec", fake_exec)
    panel.ui.campaign_list.clearSelection()
    panel._on_context_menu(QPoint(0, 0))

    assert "New Campaign…" in captured["actions"]


def test_context_menu_new_campaign_action_triggers_on_new(
    panel: CampaignsPanel, project_with_campaign, monkeypatch
):
    from PySide6.QtWidgets import QMenu

    proj, campaign = project_with_campaign
    called = []
    monkeypatch.setattr(panel, "_on_new", lambda: called.append(True))
    index = panel._model.indexFromItem(panel._model.item(0))
    panel.ui.campaign_list.setCurrentIndex(index)

    original_exec = QMenu.exec
    captured = {}

    def fake_exec(self, *args, **kwargs):
        captured["actions"] = [a.text() for a in self.actions()]
        for action in self.actions():
            if action.text() == "New Campaign…":
                action.trigger()
        return None

    monkeypatch.setattr(QMenu, "exec", fake_exec)
    try:
        panel._on_context_menu(panel.ui.campaign_list.visualRect(index).center())
    finally:
        monkeypatch.setattr(QMenu, "exec", original_exec)

    assert "New Campaign…" in captured["actions"]
    assert called == [True]


def test_open_campaign_folder_uses_desktop_services(
    panel: CampaignsPanel, project_with_campaign, monkeypatch
):
    _, campaign = project_with_campaign
    opened = []
    monkeypatch.setattr(
        "pam_analyzer.ui.panels.campaigns_panel.QDesktopServices.openUrl",
        lambda url: opened.append(url),
    )
    panel._open_campaign_folder(campaign)
    assert len(opened) == 1
    assert Path(opened[0].toLocalFile()) == campaign.folder


def test_inventory_clears_when_project_switches(
    qtbot, panel: CampaignsPanel, state: AppState, project_with_campaign, tmp_path: Path
):
    _proj, campaign = project_with_campaign
    (campaign.folder / "MSD-X" / "week_01").mkdir(parents=True)
    (campaign.folder / "MSD-X" / "week_01" / "a.WAV").write_bytes(b"\x00" * 16)
    state.refresh_audio_inventory()
    assert state.audio_inventory.for_campaign("alpha") is not None

    other_audio = tmp_path / "audio2"
    other_audio.mkdir()
    other = Project(path=tmp_path / "other.pamproj", audio_recordings_path=other_audio)
    TomlProjectRepository().save(other)
    state.load_project(other.path)

    assert state.audio_inventory.campaigns == ()


def test_new_button_clears_selection_and_shows_form(qtbot, panel: CampaignsPanel):
    panel.ui.new_button.click()
    assert panel._detail.ui.stack.currentWidget() is panel._detail.ui.form_page
    assert panel._detail.ui.name_edit.text() == ""


def test_project_close_clears_list(panel: CampaignsPanel):
    panel._app_state.close_project()
    assert panel._model.rowCount() == 0
    assert panel._detail.ui.stack.currentWidget() is panel._detail.ui.empty_page


def test_create_campaign_appears_in_list(qtbot, panel: CampaignsPanel, project_with_campaign):
    proj, _ = project_with_campaign
    panel.ui.new_button.click()
    panel._detail.ui.name_edit.setText("beta")
    # Simulate a map click by directly setting _location_set
    panel._detail._location_set = True
    panel._detail._validate()
    assert panel._detail.ui.save_button.isEnabled()
    panel._detail.ui.save_button.click()
    qtbot.waitUntil(lambda: panel._model.rowCount() == 2, timeout=2000)
    names = [panel._model.item(r).text() for r in range(panel._model.rowCount())]
    assert "beta" in names


def test_delete_confirm_page_shows_audio_count(qtbot, panel: CampaignsPanel, project_with_campaign):
    _, campaign = project_with_campaign
    (campaign.folder / "rec.wav").write_bytes(b"RIFF")
    index = panel._model.index(0, 0)
    panel.ui.campaign_list.setCurrentIndex(index)
    panel._show_delete_confirm(campaign)
    assert panel._detail.ui.stack.currentWidget() is panel._detail.ui.confirm_page
    assert "1 audio file" in panel._detail.ui.confirm_label.text()


def test_map_widget_set_location_calls_qml(panel: CampaignsPanel, monkeypatch):
    """set_location should delegate to the QML rootObject.setMarker method."""
    from unittest.mock import MagicMock

    mock_root = MagicMock()
    monkeypatch.setattr(panel._detail._map._qw, "rootObject", lambda: mock_root)

    panel._detail._map.set_location(48.0, 11.0)

    mock_root.setMarker.assert_called_once()
    call_args = mock_root.setMarker.call_args
    assert call_args[0][0] == 48.0
    assert call_args[0][1] == 11.0


def test_map_widget_clear_calls_qml(panel: CampaignsPanel, monkeypatch):
    """clear should delegate to the QML rootObject.clearMarker method."""
    from unittest.mock import MagicMock

    mock_root = MagicMock()
    monkeypatch.setattr(panel._detail._map._qw, "rootObject", lambda: mock_root)

    panel._detail._map.clear()

    mock_root.clearMarker.assert_called_once()
