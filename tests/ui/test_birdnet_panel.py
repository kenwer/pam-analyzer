"""Smoke tests for the BirdNET panel."""

import dataclasses
import threading
from pathlib import Path

import pytest

from pam_analyzer.domain import (
    AnalysisProgressSnapshot,
    AnalysisRunResult,
    Campaign,
    CampaignRunResult,
    FilterMode,
    LatLon,
    Project,
    RunStatus,
)
from pam_analyzer.infrastructure.http_download import DownloadCancelled, DownloadError
from pam_analyzer.ui.app_state import AppState
from pam_analyzer.ui.model_downloads import ModelDownloads
from pam_analyzer.ui.panels.birdnet_panel import BirdNetPanel, _campaign_names_caption
from tests.conftest import ALT_MODEL_KEY, DEFAULT_MODEL_KEY, FAKE_MODEL_INFO

_MB = 1024 * 1024


class _FakeRunner:
    def __init__(self, model_key: str = DEFAULT_MODEL_KEY) -> None:
        self.model_key = model_key
        self.model_info = dataclasses.replace(
            FAKE_MODEL_INFO, name=f"{model_key} model", info_url=f"https://example.org/{model_key}"
        )
        self.download_bytes = 0
        self.release = threading.Event()  # lets install_model() finish
        self.download_error: str | None = None

    def missing_download_bytes(self) -> int:
        return self.download_bytes

    def install_model(self, progress, cancelled) -> None:
        progress(3 * _MB, 10 * _MB)
        while not self.release.wait(0.01):
            if cancelled():
                raise DownloadCancelled
        if self.download_error is not None:
            raise DownloadError(self.download_error)
        self.download_bytes = 0

    def count_audio_files(self, _path: Path) -> int:
        return 0

    def available_locales(self) -> list[str]:
        return ["en", "de", "fr"]

    def run(self, **kwargs) -> AnalysisRunResult:
        return AnalysisRunResult(status=RunStatus.COMPLETED)


def _runners() -> dict[str, object]:
    """Both shipped engines, in the order the real app registers them.

    The first key is the panel's default selection, so keeping v3.0 first
    matches production and keeps the single-engine expectations in the tests
    below valid.
    """
    return {
        DEFAULT_MODEL_KEY: _FakeRunner(DEFAULT_MODEL_KEY),
        ALT_MODEL_KEY: _FakeRunner(ALT_MODEL_KEY),
    }


def _make_panel(state: AppState, runners: dict[str, object] | None = None) -> BirdNetPanel:
    """A panel with its own download controller, as the main window would hand it one."""
    downloads = ModelDownloads()
    p = BirdNetPanel(state, _runners() if runners is None else runners, downloads)
    downloads.setParent(p)
    return p


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
        c.create()
    proj = Project(folder=project_folder)
    proj.save()
    return proj, campaigns


@pytest.fixture
def state(project_and_campaigns) -> AppState:
    return AppState()


@pytest.fixture
def panel(qtbot, state: AppState, project_and_campaigns, load_project) -> BirdNetPanel:
    proj, _ = project_and_campaigns
    p = _make_panel(state)
    qtbot.addWidget(p)
    load_project(state, proj.folder)
    return p


def test_panel_loads_disabled_without_project(qtbot):
    state = AppState()
    p = _make_panel(state)
    qtbot.addWidget(p)

    assert not p.ui.run_button.isEnabled()
    assert not p.ui.campaign_combo.isEnabled()


def test_campaign_combo_populates_on_project_load(panel: BirdNetPanel, project_and_campaigns):
    _proj, campaigns = project_and_campaigns
    combo = panel.ui.campaign_combo
    # "All campaigns" item + one per campaign
    assert combo.count() == len(campaigns) + 1
    assert combo.itemData(0) == "__all__"


def test_campaign_caption_shows_location_for_location_campaign(panel: BirdNetPanel):
    # Discover order is most-recent first, so "beta" (LIST) is index 1, "alpha" (LOCATION) is 2.
    combo = panel.ui.campaign_combo
    alpha_idx = next(i for i in range(combo.count()) if combo.itemData(i) == "alpha")
    combo.setCurrentIndex(alpha_idx)
    assert "Location" in panel.ui.campaign_caption_label.text()


def test_campaign_caption_shows_species_list_for_list_campaign(panel: BirdNetPanel):
    combo = panel.ui.campaign_combo
    beta_idx = next(i for i in range(combo.count()) if combo.itemData(i) == "beta")
    combo.setCurrentIndex(beta_idx)
    assert "Species list" in panel.ui.campaign_caption_label.text()


def _make_completed_outcome(state: AppState, count: int = 42) -> AnalysisRunResult:
    """Plant a real CSV inside the alpha campaign folder so the disk-discovery
    triggered by _on_finished picks it up, and return a matching COMPLETED
    AnalysisRunResult for the run that produced it."""
    project = state.project
    assert project is not None
    campaign_dir = project.folder / "alpha"
    campaign_dir.mkdir(parents=True, exist_ok=True)
    (campaign_dir / "campaign.toml").touch()
    csv_path = campaign_dir / f"detections-{DEFAULT_MODEL_KEY}.csv"
    csv_path.write_text(
        "Species,Confidence\n" + "Robin,0.9\n" * count,
        encoding="utf-8",
    )
    return AnalysisRunResult(
        status=RunStatus.COMPLETED,
        campaigns=(
            CampaignRunResult(
                campaign_name="alpha",
                detections_csv=csv_path,
                detection_count=count,
                wav_count=10,
                aru_count=2,
                elapsed=1.5,
            ),
        ),
        elapsed=1.5,
    )


def test_on_finished_switches_to_results_page(panel: BirdNetPanel, state: AppState):
    result = _make_completed_outcome(state)
    panel._on_finished(result)

    assert panel.ui.status_stack.currentIndex() == 2  # page_results
    assert "42" in panel.ui.summary_label.text()


def test_completed_run_toasts_with_a_link_to_the_results(panel: BirdNetPanel, state: AppState, toasts):
    panel._on_finished(_make_completed_outcome(state, count=1234))

    [(kind, _title, text, kwargs)] = toasts
    assert kind == "success"
    assert "1,234 detections in 1 campaign." in text
    assert kwargs["duration"] == 0
    requested: list[bool] = []
    panel.showResultsRequested.connect(lambda: requested.append(True))
    dict(kwargs["links"])["Show results"]()
    assert requested == [True]


def test_failed_run_stays_modal(panel: BirdNetPanel, monkeypatch, toasts):
    warnings: list[tuple] = []
    monkeypatch.setattr("pam_analyzer.ui.panels.birdnet_panel.QMessageBox.warning", lambda *a: warnings.append(a))
    panel._on_finished(AnalysisRunResult(status=RunStatus.FAILED, error="out of memory"))

    assert [w[1:] for w in warnings] == [("Analysis failed", "out of memory")]
    assert toasts == []


def test_cancelled_run_still_shows_completed_campaigns(panel: BirdNetPanel, state: AppState):
    """A cancelled run must surface the campaigns that finished before the
    cancel. Their CSVs are already on disk, so the panel shows the results
    page, not a blank idle page. Regression test for the original bug: a
    cancel over a multi-campaign run wiped the summary of the completed ones.
    """
    _make_completed_outcome(state, count=7)
    # The runner stopped early: it carries no in-memory campaigns, only the
    # CANCELLED outcome. The completed work is discovered from disk.
    panel._on_finished(AnalysisRunResult(status=RunStatus.CANCELLED))

    assert panel.ui.status_stack.currentIndex() == 2  # page_results
    assert "7" in panel.ui.summary_label.text()


def test_cancelled_before_any_csv_returns_to_idle(panel: BirdNetPanel, state: AppState):
    """A cancel before any campaign wrote a CSV (and nothing on disk from a
    prior run) drops back to the idle page rather than lingering on progress.
    """
    panel.ui.status_stack.setCurrentIndex(1)  # progress page, as during a run
    panel._on_finished(AnalysisRunResult(status=RunStatus.CANCELLED))

    assert panel.ui.status_stack.currentIndex() == 0  # page_idle


def test_loads_previous_results_from_disk(qtbot, tmp_path: Path, load_project):
    """Opening a project that already has detection CSVs should surface them
    in the BirdNET panel without the user re-running analysis."""
    proj = Project(folder=tmp_path / "loaded")
    proj.save()

    campaign_dir = proj.folder / "alpha"
    campaign_dir.mkdir(parents=True)
    (campaign_dir / "campaign.toml").touch()
    csv_path = campaign_dir / f"detections-{DEFAULT_MODEL_KEY}.csv"
    csv_path.write_text(
        "Species,Confidence\nRobin,0.9\nSparrow,0.8\nCrow,0.7\n",
        encoding="utf-8",
    )

    state = AppState()
    panel = _make_panel(state)
    qtbot.addWidget(panel)

    load_project(state, proj.folder)

    assert panel.ui.status_stack.currentIndex() == 2  # page_results
    assert state.analysis_inventory is not None
    assert len(state.analysis_inventory.campaigns) == 1
    # A single model carries no per-model breakdown (it would repeat the total).
    assert "3 detections" in panel.ui.summary_label.text()
    assert "[" not in panel.ui.summary_label.text()


def test_panel_shows_csvs_from_models_no_longer_shipped(qtbot, tmp_path: Path, load_project):
    """All CSVs in a campaign are listed at once, whatever produced them.

    A campaign analyzed before the move to BirdNET v3.0 still holds its
    BirdNET-2.4 and Perch-2.0 files. Those are never rewritten, so the panel
    has to keep reading them. The Model column and the filename suffix are
    what tell the user which model each row came from.
    """
    proj = Project(folder=tmp_path / "dual")
    proj.save()

    campaign_dir = proj.folder / "alpha"
    campaign_dir.mkdir(parents=True)
    (campaign_dir / "campaign.toml").touch()
    bn = campaign_dir / "detections-BirdNET-2.4.csv"
    bn.write_text("Species,Confidence\nRobin,0.9\n", encoding="utf-8")
    pc = campaign_dir / "detections-Perch-2.0.csv"
    pc.write_text("Species,Confidence\nCrow,0.7\nJay,0.6\n", encoding="utf-8")

    state = AppState()
    panel = _make_panel(state)
    qtbot.addWidget(panel)
    load_project(state, proj.folder)

    # Both rows visible: 1 (BirdNET 2.4) + 2 (Perch) = 3 detections, even
    # though neither model ships any more.
    assert "3 detections" in panel.ui.summary_label.text()
    assert panel._results_model.rowCount() == 2


def test_fresh_run_keeps_legacy_model_csvs_visible(qtbot, tmp_path: Path, load_project):
    """A finished run leaves CSVs from retired models visible alongside it.

    Regression test: the in-memory result used to be replaced with only the
    fresh run's rows, so sibling CSVs vanished from the view. It also covers
    the upgrade path, where a campaign carries detections from BirdNET 2.4
    and Perch 2.0 that the app can still read but can no longer produce.
    """
    proj = Project(folder=tmp_path / "seq")
    proj.save()
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

    state = AppState()
    panel = _make_panel(state)
    qtbot.addWidget(panel)
    load_project(state, proj.folder)

    # Simulate Perch finishing: the runner has already written its CSV, and
    # _on_finished triggers a fresh on-disk discovery.
    fresh_perch = AnalysisRunResult(
        status=RunStatus.COMPLETED,
        campaigns=(
            CampaignRunResult(
                campaign_name="alpha",
                detections_csv=perch_csv,
                detection_count=1,
                wav_count=1,
                aru_count=1,
                elapsed=0.5,
            ),
        ),
        elapsed=0.5,
    )
    panel._on_finished(fresh_perch)

    # Both CSVs are present: 2 BirdNET detections + 1 Perch = 3.
    assert panel.ui.status_stack.currentIndex() == 2  # page_results
    assert panel._results_model.rowCount() == 2
    # The summary breaks the totals down per model (ordered by count), and
    # singularizes "1 detection" / "1 CSV".
    text = panel.ui.summary_label.text()
    assert "3 detections  ·  2 CSVs" in text
    assert "[BirdNET-2.4: 2 detections, 1 CSV]   [Perch-2.0: 1 detection, 1 CSV]" in text


def test_project_switch_clears_stale_results(panel: BirdNetPanel, state: AppState, tmp_path: Path, load_project):
    """Opening a different project must drop the previous project's results.

    This locks in the cure for the original bug: panels showed stale BirdNET
    results from the previously opened project.
    """
    panel._on_finished(_make_completed_outcome(state))
    assert panel.ui.status_stack.currentIndex() == 2  # page_results
    assert state.analysis_inventory is not None

    # Build a second project on disk and switch to it.
    other = Project(folder=tmp_path / "other")
    other.save()
    load_project(state, other.folder)

    assert state.analysis_inventory is None
    assert panel.ui.status_stack.currentIndex() == 0  # page_idle
    assert panel._results_model.rowCount() == 0
    assert panel.ui.summary_label.text() == ""


def test_model_combo_lists_both_engines(panel: BirdNetPanel):
    combo = panel.ui.model_combo
    assert [combo.itemData(i) for i in range(combo.count())] == [
        DEFAULT_MODEL_KEY,
        ALT_MODEL_KEY,
    ]
    # First entry wins for a project that names no model.
    assert panel.ui.run_button.text() == f"Run {DEFAULT_MODEL_KEY}"


def test_selecting_a_model_switches_runner_and_persists(panel: BirdNetPanel, project_and_campaigns):
    """Picking an engine has to survive reopening the project.

    The panel writes the choice straight to the project TOML rather than to
    app settings, so the model travels with the study folder.
    """
    proj, _ = project_and_campaigns
    combo = panel.ui.model_combo
    combo.setCurrentIndex(combo.findData(ALT_MODEL_KEY))

    assert panel._runner.model_key == ALT_MODEL_KEY
    assert panel.ui.run_button.text() == f"Run {ALT_MODEL_KEY}"
    assert Project.load(proj.folder).analysis_model == ALT_MODEL_KEY


def test_project_saved_with_an_unknown_model_falls_back(qtbot, tmp_path, load_project):
    """A project naming a retired engine (e.g. Perch) must still open.

    The stored value is left untouched rather than overwritten, so the
    project keeps its record of what actually produced the existing CSVs.
    """
    folder = tmp_path / "retired"
    folder.mkdir()
    Project(folder=folder, analysis_model="Perch-2.0").save()

    state = AppState()
    p = _make_panel(state)
    qtbot.addWidget(p)
    load_project(state, folder)

    assert p._runner.model_key == DEFAULT_MODEL_KEY
    assert Project.load(folder).analysis_model == "Perch-2.0"


def _snap(phase: str, done: int, total: int, detail: str | None = None) -> AnalysisProgressSnapshot:
    return AnalysisProgressSnapshot(
        campaign="",
        campaign_index=0,
        total_campaigns=2,
        files_done=done,
        files_total=total,
        phase=phase,
        phase_detail=detail,
    )


def test_model_caption_shows_selected_runner(panel: BirdNetPanel):
    assert f'href="https://example.org/{DEFAULT_MODEL_KEY}"' in panel.ui.model_caption_label.text()
    combo = panel.ui.model_combo
    combo.setCurrentIndex(combo.findData(ALT_MODEL_KEY))
    assert f'href="https://example.org/{ALT_MODEL_KEY}"' in panel.ui.model_caption_label.text()


def test_campaign_caption_lists_all_campaigns(panel: BirdNetPanel):
    assert panel.ui.campaign_combo.currentData() == "__all__"
    caption = panel.ui.campaign_caption_label.text()
    assert "alpha" in caption and "beta" in caption
    assert panel.ui.campaign_caption_label.toolTip() == ""


def test_campaign_caption_shortens_a_long_list():
    names = [f"c{i}" for i in range(28)]
    assert _campaign_names_caption(names) == "c0, c1, c2 +25 more"
    assert _campaign_names_caption(names[:3]) == "c0, c1, c2"


def test_info_panel_not_downloaded_status(qtbot, state: AppState):
    runners = _runners()
    runners[DEFAULT_MODEL_KEY].download_bytes = 5 * 1024 * 1024
    p = _make_panel(state, runners)
    qtbot.addWidget(p)
    assert p._model_status.status_label.text() == "Model not downloaded (5 MB)"


def test_run_download_updates_the_progress_page(panel: BirdNetPanel):
    panel._on_download_progress(3 * _MB, 10 * _MB)
    assert panel.ui.progress_label.text() == f"Downloading {DEFAULT_MODEL_KEY} model  ·  3 / 10 MB"
    assert (panel.ui.progress_bar.value(), panel.ui.progress_bar.maximum()) == (3, 10)


def test_info_refreshes_after_run(panel: BirdNetPanel):
    runner = panel._runner
    runner.download_bytes = 5 * 1024 * 1024
    panel._refresh_model_status()
    assert panel._model_status.status_label.text().startswith("Model not downloaded")
    runner.download_bytes = 0
    panel._on_finished(AnalysisRunResult(status=RunStatus.CANCELLED))
    assert panel._model_status.status_label.text() == ""


def test_progress_page_prepares_once_the_run_download_completes(panel: BirdNetPanel):
    panel._on_download_progress(3 * _MB, 10 * _MB)
    panel._on_download_progress(10 * _MB, 10 * _MB)
    assert panel.ui.progress_label.text() == "Preparing…"
    assert panel.ui.progress_bar.maximum() == 0


@pytest.fixture
def missing_panel(qtbot, state: AppState, project_and_campaigns, load_project):
    """A panel whose two engines both still need 5 MB.

    Not handed to qtbot.addWidget: pytest-qt deletes those widgets before
    fixture teardown, and a deleted panel never receives the finished signal
    of a download still running.
    """
    runners = _runners()
    for runner in runners.values():
        runner.download_bytes = 5 * 1024 * 1024
    p = _make_panel(state, runners)
    load_project(state, project_and_campaigns[0].folder)
    yield p
    for runner in runners.values():
        runner.release.set()
    qtbot.waitUntil(lambda: not p._downloads.labels(), timeout=5000)
    p.deleteLater()


def _status(panel: BirdNetPanel) -> str:
    return panel._model_status.status_label.text()


def _select(panel: BirdNetPanel, key: str) -> None:
    panel.ui.model_combo.setCurrentIndex(panel.ui.model_combo.findData(key))


def test_download_link_downloads_in_the_background(qtbot, missing_panel: BirdNetPanel):
    missing_panel._model_status.downloadRequested.emit()
    qtbot.waitUntil(lambda: _status(missing_panel) == "Downloading model 3 / 10 MB", timeout=5000)
    assert missing_panel.ui.model_combo.isEnabled()
    assert ">Cancel</a>" in missing_panel._model_status.action_label.text()
    missing_panel._runner.release.set()
    qtbot.waitUntil(lambda: _status(missing_panel) == "", timeout=5000)
    assert missing_panel._model_status.action_label.isHidden()


def test_switching_engines_keeps_each_download_running(qtbot, missing_panel: BirdNetPanel):
    missing_panel._model_status.downloadRequested.emit()
    qtbot.waitUntil(lambda: _status(missing_panel) == "Downloading model 3 / 10 MB", timeout=5000)
    _select(missing_panel, ALT_MODEL_KEY)
    assert _status(missing_panel) == "Model not downloaded (5 MB)"
    missing_panel._model_status.downloadRequested.emit()
    qtbot.waitUntil(lambda: _status(missing_panel) == "Downloading model 3 / 10 MB", timeout=5000)
    _select(missing_panel, DEFAULT_MODEL_KEY)
    assert _status(missing_panel) == "Downloading model 3 / 10 MB"
    assert len(missing_panel._downloads.labels()) == 2


def test_cancel_link_stops_the_download(qtbot, missing_panel: BirdNetPanel):
    missing_panel._model_status.downloadRequested.emit()
    qtbot.waitUntil(lambda: _status(missing_panel) == "Downloading model 3 / 10 MB", timeout=5000)
    missing_panel._model_status.cancelRequested.emit()
    qtbot.waitUntil(lambda: _status(missing_panel) == "Model not downloaded (5 MB)", timeout=5000)
    assert not missing_panel._downloads.labels()


def test_failed_download_toasts_and_offers_the_link_again(qtbot, missing_panel: BirdNetPanel, toasts):
    missing_panel._runner.download_error = "huggingface.co answered 503 Service Unavailable."
    missing_panel._model_status.downloadRequested.emit()
    missing_panel._runner.release.set()
    qtbot.waitUntil(lambda: bool(toasts), timeout=5000)
    [(kind, title, text, _kwargs)] = toasts
    assert kind == "warning"
    assert title == "Model download failed"
    assert "503" in text
    assert _status(missing_panel) == "Model not downloaded (5 MB)"
    assert ">Download now</a>" in missing_panel._model_status.action_label.text()


def test_run_takes_over_a_background_download(qtbot, missing_panel: BirdNetPanel):
    """Run cancels the background download and installs as its own first step, so Stop cancels it."""
    missing_panel._model_status.downloadRequested.emit()
    qtbot.waitUntil(lambda: _status(missing_panel) == "Downloading model 3 / 10 MB", timeout=5000)
    missing_panel.ui.run_button.click()
    qtbot.waitUntil(lambda: not missing_panel._downloads.labels(), timeout=5000)
    expected = f"Downloading {DEFAULT_MODEL_KEY} model  ·  3 / 10 MB"
    qtbot.waitUntil(lambda: missing_panel.ui.progress_label.text() == expected, timeout=5000)
    missing_panel._runner.release.set()
    qtbot.waitUntil(lambda: not missing_panel._state.running, timeout=5000)


def test_caption_has_licence_and_link(panel: BirdNetPanel):
    info = panel._runner.model_info
    caption = panel.ui.model_caption_label.text()
    assert caption.startswith(f"{info.licence} · ")
    assert f'href="{info.info_url}"' in caption
    # Word joiners after slashes keep the link on one line
    assert f">{info.info_url.removeprefix('https://')}</a>" in caption.replace("\u2060", "")
    assert "/\u2060" in caption


def test_running_downloads_are_named_for_the_quit_prompt(qtbot, missing_panel: BirdNetPanel):
    assert missing_panel._downloads.labels() == []
    missing_panel._model_status.downloadRequested.emit()
    assert missing_panel._downloads.labels() == [f"{DEFAULT_MODEL_KEY} model model download"]
