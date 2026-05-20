"""pytest-qt smoke tests for the Campaigns panel."""

from pathlib import Path

import pytest

from pam_analyzer.domain import Campaign, FilterMode, LatLon, Project
from pam_analyzer.infrastructure import TomlCampaignRepository, TomlProjectRepository
from pam_analyzer.ui.app_state import AppState
from pam_analyzer.ui.panels.campaigns_panel import CampaignsPanel


@pytest.fixture(autouse=True)
def _isolated_qsettings(tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QSettings

    QCoreApplication.setOrganizationName("PAMAnalyzerTest")
    QCoreApplication.setApplicationName(f"PAMAnalyzerTest-{tmp_path.name}")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "qsettings"))
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(tmp_path / "qsettings"),
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
def panel(qtbot, state: AppState, project_with_campaign) -> CampaignsPanel:
    proj, _ = project_with_campaign
    p = CampaignsPanel(state, TomlCampaignRepository())
    qtbot.addWidget(p)
    state.load_project(proj.path)
    return p


def test_panel_shows_empty_on_no_project(qtbot):
    state = AppState(TomlProjectRepository(), TomlCampaignRepository())
    p = CampaignsPanel(state, TomlCampaignRepository())
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
        lambda: panel._detail.ui.stack.currentWidget() is panel._detail._view_page,
        timeout=1000,
    )
    assert panel._detail._view_name_label.text() == "alpha"
    # Filter summary should describe the location-mode campaign.
    assert "Location" in panel._detail._view_filter_label.text()


def test_edit_button_switches_to_form(qtbot, panel: CampaignsPanel):
    index = panel._model.index(0, 0)
    panel.ui.campaign_list.setCurrentIndex(index)
    qtbot.waitUntil(
        lambda: panel._detail.ui.stack.currentWidget() is panel._detail._view_page,
        timeout=1000,
    )
    panel._detail._view_edit_button.click()
    assert panel._detail.ui.stack.currentWidget() is panel._detail.ui.form_page
    assert panel._detail.ui.name_edit.text() == "alpha"


def test_form_cancel_returns_to_view_when_editing(qtbot, panel: CampaignsPanel):
    index = panel._model.index(0, 0)
    panel.ui.campaign_list.setCurrentIndex(index)
    qtbot.waitUntil(
        lambda: panel._detail.ui.stack.currentWidget() is panel._detail._view_page,
        timeout=1000,
    )
    panel._detail._view_edit_button.click()
    panel._detail.ui.cancel_button.click()
    assert panel._detail.ui.stack.currentWidget() is panel._detail._view_page


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
        lambda: panel._detail.ui.stack.currentWidget() is panel._detail._view_page,
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

    # Headline label mentions file count and card count.
    text = panel._detail._inventory_label.text()
    assert "2" in text
    assert "card" in text


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
