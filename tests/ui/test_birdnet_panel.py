"""Smoke tests for the BirdNET panel."""

from pathlib import Path

import pytest

from pam_analyzer.domain import (
    AnalysisRunResult,
    Campaign,
    CampaignRunResult,
    FilterMode,
    LatLon,
    Project,
)
from pam_analyzer.infrastructure import TomlCampaignRepository, TomlProjectRepository
from pam_analyzer.ui.app_state import AppState
from pam_analyzer.ui.panels.birdnet_panel import BirdNetPanel


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


class _FakeRunner:
    def count_audio_files(self, _path: Path) -> int:
        return 0

    def available_locales(self) -> list[str]:
        return ["en", "de", "fr"]

    def run(self, **kwargs) -> AnalysisRunResult:
        return AnalysisRunResult(campaigns=(), elapsed=0.0)


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
def panel(qtbot, state: AppState, project_and_campaigns) -> BirdNetPanel:
    proj, _ = project_and_campaigns
    p = BirdNetPanel(state, _FakeRunner(), TomlCampaignRepository())
    qtbot.addWidget(p)
    state.load_project(proj.path)
    return p


def test_panel_loads_disabled_without_project(qtbot):
    state = AppState(TomlProjectRepository(), TomlCampaignRepository())
    p = BirdNetPanel(state, _FakeRunner(), TomlCampaignRepository())
    qtbot.addWidget(p)

    assert not p.ui.run_button.isEnabled()
    assert not p.ui.min_conf_slider.isEnabled()


def test_combo_populates_on_project_load(panel: BirdNetPanel, project_and_campaigns):
    _proj, campaigns = project_and_campaigns
    combo = panel.ui.campaign_combo
    # "All campaigns" item + one per campaign
    assert combo.count() == len(campaigns) + 1
    assert combo.itemData(0) == "__all__"


def test_filter_info_shows_location_for_location_campaign(panel: BirdNetPanel):
    # Discover order is most-recent first, so "beta" (LIST) is index 1, "alpha" (LOCATION) is 2.
    combo = panel.ui.campaign_combo
    alpha_idx = next(i for i in range(combo.count()) if combo.itemData(i) == "alpha")
    combo.setCurrentIndex(alpha_idx)
    assert "Location" in panel.ui.filter_info_label.text()


def test_filter_info_shows_species_list_for_list_campaign(panel: BirdNetPanel):
    combo = panel.ui.campaign_combo
    beta_idx = next(i for i in range(combo.count()) if combo.itemData(i) == "beta")
    combo.setCurrentIndex(beta_idx)
    assert "Species list" in panel.ui.filter_info_label.text()


def test_slider_autosave_calls_update_birdnet_settings(panel: BirdNetPanel, state: AppState):
    saved_args: list[dict] = []
    original = state.update_birdnet_settings

    def spy(**kwargs):
        saved_args.append(kwargs)
        original(**kwargs)

    state.update_birdnet_settings = spy  # type: ignore[method-assign]
    panel.ui.min_conf_slider.setValue(60)

    assert any("min_conf" in a for a in saved_args)
    assert abs(saved_args[-1]["min_conf"] - 0.60) < 1e-9


def _make_result(tmp_path: Path, count: int = 42) -> AnalysisRunResult:
    dummy_csv = tmp_path / "dummy.csv"
    dummy_csv.touch()
    return AnalysisRunResult(
        campaigns=(
            CampaignRunResult(
                campaign_name="alpha",
                output_dir=tmp_path,
                detections_csv=dummy_csv,
                per_aru_csv=dummy_csv,
                all_arus_csv=dummy_csv,
                species_list_txt=None,
                week_results=(),
                detection_count=count,
                wav_count=10,
                aru_count=2,
                elapsed=1.5,
            ),
        ),
        elapsed=1.5,
    )


def test_on_succeeded_switches_to_results_page(panel: BirdNetPanel, tmp_path: Path):
    result = _make_result(tmp_path)
    panel._on_succeeded(result)

    assert panel.ui.status_stack.currentIndex() == 2  # page_results
    assert "42" in panel.ui.summary_label.text()


def test_loads_previous_results_from_disk(qtbot, tmp_path: Path):
    """Opening a project that already has detection CSVs should surface them
    in the BirdNET panel without the user re-running analysis."""
    audio_root = tmp_path / "audio"
    audio_root.mkdir()
    proj = Project(path=tmp_path / "loaded.pamproj", audio_recordings_path=audio_root)
    TomlProjectRepository().save(proj)

    output_base = proj.output_base
    campaign_dir = output_base / "alpha"
    campaign_dir.mkdir(parents=True)
    csv_path = campaign_dir / "alpha-detections.csv"
    csv_path.write_text(
        "Species,Confidence\n"
        "Robin,0.9\n"
        "Sparrow,0.8\n"
        "Crow,0.7\n",
        encoding="utf-8",
    )

    state = AppState(TomlProjectRepository(), TomlCampaignRepository())
    panel = BirdNetPanel(state, _FakeRunner(), TomlCampaignRepository())
    qtbot.addWidget(panel)

    state.load_project(proj.path)

    assert panel.ui.status_stack.currentIndex() == 2  # page_results
    assert state.last_analysis_result is not None
    assert state.last_analysis_result.from_disk is True
    assert "Loaded previous results" in panel.ui.summary_label.text()
    assert "3 detections" in panel.ui.summary_label.text()


def test_project_switch_clears_stale_results(
    panel: BirdNetPanel, state: AppState, tmp_path: Path
):
    """Opening a different project must drop the previous project's results.

    This locks in the cure for the original bug: panels showed stale BirdNET
    results from the previously opened project.
    """
    panel._on_succeeded(_make_result(tmp_path))
    assert panel.ui.status_stack.currentIndex() == 2  # page_results
    assert state.last_analysis_result is not None

    # Build a second project on disk and switch to it.
    other_root = tmp_path / "audio2"
    other_root.mkdir()
    other = Project(path=tmp_path / "other.pamproj", audio_recordings_path=other_root)
    TomlProjectRepository().save(other)
    state.load_project(other.path)

    assert state.last_analysis_result is None
    assert panel.ui.status_stack.currentIndex() == 0  # page_idle
    assert panel._results_model.rowCount() == 0
    assert panel.ui.summary_label.text() == ""
