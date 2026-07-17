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


class _FakeRunner:
    model_key = "BirdNET-2.4"

    def count_audio_files(self, _path: Path) -> int:
        return 0

    def available_locales(self) -> list[str]:
        return ["en", "de", "fr"]

    def run(self, **kwargs) -> AnalysisRunResult:
        return AnalysisRunResult(campaigns=(), elapsed=0.0)


@pytest.fixture
def project_and_campaigns(tmp_path: Path):
    project_folder = tmp_path / "proj"
    project_folder.mkdir()
    campaigns = [
        Campaign(
            name="alpha",
            folder=project_folder / "alpha",
            species_filter_mode=FilterMode.LOCATION,
            location=LatLon(48.0, 11.0),
        ),
        Campaign(
            name="beta",
            folder=project_folder / "beta",
            species_filter_mode=FilterMode.LIST,
        ),
    ]
    for c in campaigns:
        TomlCampaignRepository().create(c)
    proj = Project(folder=project_folder)
    TomlProjectRepository().save(proj)
    return proj, campaigns


@pytest.fixture
def state(project_and_campaigns) -> AppState:
    return AppState(TomlProjectRepository(), TomlCampaignRepository())


@pytest.fixture
def panel(qtbot, state: AppState, project_and_campaigns) -> BirdNetPanel:
    proj, _ = project_and_campaigns
    p = BirdNetPanel(state, {"BirdNET-2.4": _FakeRunner()}, TomlCampaignRepository())
    qtbot.addWidget(p)
    state.load_project(proj.folder)
    return p


def test_panel_loads_disabled_without_project(qtbot):
    state = AppState(TomlProjectRepository(), TomlCampaignRepository())
    p = BirdNetPanel(state, {"BirdNET-2.4": _FakeRunner()}, TomlCampaignRepository())
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


def _make_result_on_disk(state: AppState, count: int = 42) -> AnalysisRunResult:
    """Plant a real CSV inside the alpha campaign folder so the disk-discovery
    triggered by _on_succeeded picks it up, and return a matching in-memory
    AnalysisRunResult for completeness."""
    project = state.project
    assert project is not None
    campaign_dir = project.folder / "alpha"
    campaign_dir.mkdir(parents=True, exist_ok=True)
    (campaign_dir / "campaign.toml").touch()
    csv_path = campaign_dir / "detections-BirdNET-2.4.csv"
    csv_path.write_text(
        "Species,Confidence\n" + "Robin,0.9\n" * count,
        encoding="utf-8",
    )
    return AnalysisRunResult(
        campaigns=(
            CampaignRunResult(
                campaign_name="alpha",
                output_dir=campaign_dir,
                detections_csv=csv_path,
                species_list_txt=None,
                detection_count=count,
                wav_count=10,
                aru_count=2,
                elapsed=1.5,
                model_key="BirdNET-2.4",
            ),
        ),
        elapsed=1.5,
    )


def test_on_succeeded_switches_to_results_page(panel: BirdNetPanel, state: AppState):
    result = _make_result_on_disk(state)
    panel._on_succeeded(result)

    assert panel.ui.status_stack.currentIndex() == 2  # page_results
    assert "42" in panel.ui.summary_label.text()


def test_loads_previous_results_from_disk(qtbot, tmp_path: Path):
    """Opening a project that already has detection CSVs should surface them
    in the BirdNET panel without the user re-running analysis."""
    proj = Project(folder=tmp_path / "loaded")
    TomlProjectRepository().save(proj)

    campaign_dir = proj.folder / "alpha"
    campaign_dir.mkdir(parents=True)
    (campaign_dir / "campaign.toml").touch()
    csv_path = campaign_dir / "detections-BirdNET-2.4.csv"
    csv_path.write_text(
        "Species,Confidence\n"
        "Robin,0.9\n"
        "Sparrow,0.8\n"
        "Crow,0.7\n",
        encoding="utf-8",
    )

    state = AppState(TomlProjectRepository(), TomlCampaignRepository())
    panel = BirdNetPanel(state, {"BirdNET-2.4": _FakeRunner()}, TomlCampaignRepository())
    qtbot.addWidget(panel)

    state.load_project(proj.folder)

    assert panel.ui.status_stack.currentIndex() == 2  # page_results
    assert state.last_analysis_result is not None
    assert len(state.last_analysis_result.campaigns) == 1
    assert "3 detections" in panel.ui.summary_label.text()


def test_panel_shows_all_csvs_regardless_of_model_selection(qtbot, tmp_path: Path):
    """All CSVs in a project are listed at once. The model combo picks what
    to run next; it does not filter the result view, because the filename
    suffix already tells the user which model each row belongs to.
    """
    proj = Project(folder=tmp_path / "dual")
    TomlProjectRepository().save(proj)

    campaign_dir = proj.folder / "alpha"
    campaign_dir.mkdir(parents=True)
    (campaign_dir / "campaign.toml").touch()
    bn = campaign_dir / "detections-BirdNET-2.4.csv"
    bn.write_text("Species,Confidence\nRobin,0.9\n", encoding="utf-8")
    pc = campaign_dir / "detections-Perch-2.0.csv"
    pc.write_text("Species,Confidence\nCrow,0.7\nJay,0.6\n", encoding="utf-8")

    state = AppState(TomlProjectRepository(), TomlCampaignRepository())
    bn_runner = _FakeRunner()
    perch_runner = _FakeRunner()
    perch_runner.model_key = "Perch-2.0"
    panel = BirdNetPanel(
        state,
        {"BirdNET-2.4": bn_runner, "Perch-2.0": perch_runner},
        TomlCampaignRepository(),
    )
    qtbot.addWidget(panel)
    state.load_project(proj.folder)

    # Both rows visible from the start: 1 (birdnet) + 2 (perch) = 3 detections.
    assert "3 detections" in panel.ui.summary_label.text()
    assert panel._results_model.rowCount() == 2

    # Switching the model selector does not filter or rearrange the rows.
    perch_idx = panel.ui.model_combo.findData("Perch-2.0")
    panel.ui.model_combo.setCurrentIndex(perch_idx)
    assert "3 detections" in panel.ui.summary_label.text()
    assert panel._results_model.rowCount() == 2


def test_panel_keeps_birdnet_after_perch_run(qtbot, tmp_path: Path):
    """After running BirdNET then Perch, both CSVs are visible. Regression
    test: previously the in-memory result was replaced with only the fresh
    run's rows, so the earlier sibling-model CSV vanished from the view.
    """
    proj = Project(folder=tmp_path / "seq")
    TomlProjectRepository().save(proj)
    campaign_dir = proj.folder / "alpha"
    campaign_dir.mkdir(parents=True)
    (campaign_dir / "campaign.toml").touch()
    # The on-disk artifact BirdNET would have written.
    (campaign_dir / "detections-BirdNET-2.4.csv").write_text(
        "Species,Confidence\nRobin,0.9\nWren,0.8\n", encoding="utf-8"
    )
    # And the artifact Perch wrote during its run.
    perch_csv = campaign_dir / "detections-Perch-2.0.csv"
    perch_csv.write_text("Species,Confidence\nCrow,0.7\n", encoding="utf-8")

    state = AppState(TomlProjectRepository(), TomlCampaignRepository())
    bn_runner = _FakeRunner()
    perch_runner = _FakeRunner()
    perch_runner.model_key = "Perch-2.0"
    panel = BirdNetPanel(
        state,
        {"BirdNET-2.4": bn_runner, "Perch-2.0": perch_runner},
        TomlCampaignRepository(),
    )
    qtbot.addWidget(panel)
    state.load_project(proj.folder)

    # Simulate Perch finishing: the runner has already written its CSV, and
    # _on_succeeded triggers a fresh on-disk discovery.
    fresh_perch = AnalysisRunResult(
        campaigns=(
            CampaignRunResult(
                campaign_name="alpha",
                output_dir=campaign_dir,
                detections_csv=perch_csv,
                species_list_txt=None,
                detection_count=1,
                wav_count=1,
                aru_count=1,
                elapsed=0.5,
                model_key="Perch-2.0",
            ),
        ),
        elapsed=0.5,
    )
    panel._on_succeeded(fresh_perch)

    # Both CSVs are present: 2 BirdNET detections + 1 Perch = 3.
    assert panel.ui.status_stack.currentIndex() == 2  # page_results
    assert "3 detections" in panel.ui.summary_label.text()
    assert panel._results_model.rowCount() == 2


def test_project_switch_clears_stale_results(
    panel: BirdNetPanel, state: AppState, tmp_path: Path
):
    """Opening a different project must drop the previous project's results.

    This locks in the cure for the original bug: panels showed stale BirdNET
    results from the previously opened project.
    """
    panel._on_succeeded(_make_result_on_disk(state))
    assert panel.ui.status_stack.currentIndex() == 2  # page_results
    assert state.last_analysis_result is not None

    # Build a second project on disk and switch to it.
    other = Project(folder=tmp_path / "other")
    TomlProjectRepository().save(other)
    state.load_project(other.folder)

    assert state.last_analysis_result is None
    assert panel.ui.status_stack.currentIndex() == 0  # page_idle
    assert panel._results_model.rowCount() == 0
    assert panel.ui.summary_label.text() == ""
