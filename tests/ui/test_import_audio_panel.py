"""Tests for ImportAudioPanel: state transitions, dropdown population, and results table."""

from pathlib import Path

import pytest

from pam_analyzer.domain import Campaign, FilterMode, LatLon, Project
from pam_analyzer.domain.audio_import import DetectedCard
from pam_analyzer.infrastructure import AudioImporter, TomlCampaignRepository, TomlProjectRepository
from pam_analyzer.ui.app_state import AppState
from pam_analyzer.ui.panels.import_audio_panel import ImportAudioPanel, ImportState


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


class _FakeScanner:
    def __init__(self) -> None:
        self._cards: list[DetectedCard] = []
        self.ejected: list[DetectedCard] = []

    def set_cards(self, cards: list[DetectedCard]) -> None:
        self._cards = list(cards)

    def scan(self, name_pattern: str) -> list[DetectedCard]:
        return list(self._cards)

    def eject(self, card: DetectedCard) -> None:
        self.ejected.append(card)


@pytest.fixture
def project_and_campaigns(tmp_path: Path):
    audio_root = tmp_path / "audio"
    audio_root.mkdir()
    campaigns = [
        Campaign(
            name="alpha",
            folder=audio_root / "alpha",
            species_filter_mode=FilterMode.LOCATION,
            location=LatLon(48.0, 11.0),
        ),
        Campaign(
            name="beta",
            folder=audio_root / "beta",
            species_filter_mode=FilterMode.LIST,
        ),
    ]
    for c in campaigns:
        TomlCampaignRepository().create(c)
    proj = Project(path=tmp_path / "demo.pamproj", audio_recordings_path=audio_root)
    TomlProjectRepository().save(proj)
    return proj, campaigns


@pytest.fixture
def state(project_and_campaigns) -> AppState:
    return AppState(TomlProjectRepository(), TomlCampaignRepository())


@pytest.fixture
def scanner() -> _FakeScanner:
    return _FakeScanner()


@pytest.fixture
def panel(qtbot, state: AppState, project_and_campaigns, scanner: _FakeScanner) -> ImportAudioPanel:
    proj, _ = project_and_campaigns
    service = AudioImporter()
    p = ImportAudioPanel(state, service, scanner)
    qtbot.addWidget(p)
    state.load_project(proj.path)
    return p


def test_panel_starts_idle_without_project(qtbot):
    state = AppState(TomlProjectRepository(), TomlCampaignRepository())
    scanner = _FakeScanner()
    p = ImportAudioPanel(state, AudioImporter(), scanner)
    qtbot.addWidget(p)

    assert p._state == ImportState.IDLE
    assert not p.ui.watch_button.isEnabled()


def test_campaign_combo_populates_on_project_load(panel: ImportAudioPanel, project_and_campaigns):
    _proj, campaigns = project_and_campaigns
    assert panel.ui.campaign_combo.count() == len(campaigns)


def test_watch_button_enabled_with_campaign(panel: ImportAudioPanel):
    assert panel.ui.campaign_combo.count() > 0
    assert panel.ui.watch_button.isEnabled()


def test_idle_to_watching_transition(panel: ImportAudioPanel):
    panel.ui.watch_button.click()
    assert panel._state == ImportState.WATCHING
    assert "Stop" in panel.ui.watch_button.text()
    assert not panel.ui.campaign_combo.isEnabled()


def test_watching_to_idle_on_stop(panel: ImportAudioPanel):
    panel.ui.watch_button.click()  # start
    panel.ui.watch_button.click()  # stop
    assert panel._state == ImportState.IDLE
    assert "Start" in panel.ui.watch_button.text()
    assert panel.ui.campaign_combo.isEnabled()


def test_poll_queues_new_cards(panel: ImportAudioPanel, scanner: _FakeScanner, tmp_path: Path):
    card_dir = tmp_path / "audio" / "alpha" / "MSD-TEST"
    card_dir.mkdir(parents=True)
    card = DetectedCard(name="MSD-TEST", mountpoint=card_dir, device="/dev/fake")
    scanner.set_cards([card])

    panel.ui.watch_button.click()
    panel._poll_timer.stop()  # prevent automatic fire during test
    panel._on_poll()

    assert panel._state == ImportState.COPYING


def test_full_import_cycle(qtbot, panel: ImportAudioPanel, scanner: _FakeScanner, tmp_path: Path):
    """IDLE -> WATCHING -> COPYING -> WATCHING with a real file copy."""
    campaign_dir = tmp_path / "audio" / "alpha"
    card_name = "MSD-TEST"
    card_dir = campaign_dir / "source_card"
    card_dir.mkdir(parents=True)

    wav = card_dir / "20240101_120000.WAV"
    wav.write_bytes(b"\x00" * 64)

    card = DetectedCard(name=card_name, mountpoint=card_dir, device="/dev/fake")
    scanner.set_cards([card])

    panel.ui.watch_button.click()
    panel._poll_timer.stop()
    panel._on_poll()

    assert panel._state == ImportState.COPYING

    with qtbot.waitSignal(panel._worker.finished, timeout=5000):
        pass

    # _on_finished runs via a queued connection; waitUntil drains the event loop.
    qtbot.waitUntil(lambda: panel._state == ImportState.WATCHING, timeout=1000)

    assert panel.ui.results_table.rowCount() == 1
    status_item = panel.ui.results_table.item(0, 6)
    assert status_item is not None
    assert status_item.text() == "OK"
    assert panel.ui.summary_label.text() != ""


def test_results_table_shows_error_on_missing_card(qtbot, panel: ImportAudioPanel, scanner: _FakeScanner, tmp_path: Path):
    card = DetectedCard(name="MISSING", mountpoint=tmp_path / "nonexistent", device="/dev/fake")
    scanner.set_cards([card])

    panel.ui.watch_button.click()
    panel._poll_timer.stop()
    panel._on_poll()

    # Missing mountpoint: list_card_files raises; result appended immediately (no thread)
    assert panel.ui.results_table.rowCount() == 1
    status = panel.ui.results_table.item(0, 6)
    assert status is not None
    assert status.text() != "OK"


def test_campaign_change_clears_seen(panel: ImportAudioPanel):
    panel.ui.watch_button.click()
    panel._poll_timer.stop()
    # Switch campaign; _queue.clear_seen() must be called
    if panel.ui.campaign_combo.count() > 1:
        panel.ui.watch_button.click()  # stop watching first
        panel.ui.campaign_combo.setCurrentIndex(1)
        # After change, queue should be clear
        assert panel._queue.pending == []
