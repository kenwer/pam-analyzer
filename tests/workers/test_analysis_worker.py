"""Verify that AnalysisWorker correctly maps Campaign objects to CampaignRunInput."""

from pathlib import Path

from pam_analyzer.domain import (
    AnalysisRunResult,
    AnalysisSettings,
    Campaign,
    CampaignRunInput,
    FilterMode,
    LatLon,
    Project,
)
from pam_analyzer.workers.analysis_worker import AnalysisWorker


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def count_audio_files(self, _path: Path) -> int:
        return 0

    def available_locales(self) -> list[str]:
        return ["en", "de"]

    def run(self, **kwargs) -> AnalysisRunResult:
        self.calls.append(kwargs)
        return AnalysisRunResult(campaigns=(), elapsed=0.0)


class FakeCampaignRepo:
    def read_species_list(self, c: Campaign) -> str:
        return f"species_list_for_{c.name}"

    def read_must_have_species(self, c: Campaign) -> str:
        return f"must_have_for_{c.name}"

    def discover(self, audio_root: Path) -> list[Campaign]:
        return []

    def load(self, name: str, folder: Path) -> Campaign:
        raise NotImplementedError

    def save(self, campaign: Campaign) -> None:
        pass

    def delete(self, campaign: Campaign) -> None:
        pass

    def create(self, campaign: Campaign) -> None:
        pass

    def rename(self, campaign: Campaign, new_name: str) -> Campaign:
        raise NotImplementedError

    def write_species_list(self, campaign: Campaign, content: str) -> None:
        pass

    def write_must_have_species(self, campaign: Campaign, content: str) -> None:
        pass

    def count_audio_files(self, campaign: Campaign) -> int:
        return 0


def _project(tmp_path: Path) -> Project:
    return Project(folder=tmp_path)


def test_run_passes_species_list_text_only_for_list_mode(tmp_path: Path, qtbot) -> None:
    runner = FakeRunner()
    campaigns = [
        Campaign(
            name="A",
            folder=tmp_path / "A",
            species_filter_mode=FilterMode.LOCATION,
            location=LatLon(48.0, 11.0),
        ),
        Campaign(
            name="B",
            folder=tmp_path / "B",
            species_filter_mode=FilterMode.LIST,
        ),
    ]
    settings = AnalysisSettings(min_conf=0.3)
    worker = AnalysisWorker(runner, FakeCampaignRepo(), _project(tmp_path), campaigns, settings)

    worker.run()

    inputs: list[CampaignRunInput] = runner.calls[0]["campaigns"]
    assert inputs[0].species_list_text is None
    assert inputs[1].species_list_text == "species_list_for_B"
    # The must-have list is the mirror image: read for LOCATION, not LIST.
    assert inputs[0].must_have_species_text == "must_have_for_A"
    assert inputs[1].must_have_species_text is None


def test_run_forwards_project_fields(tmp_path: Path, qtbot) -> None:
    runner = FakeRunner()
    proj = Project(folder=tmp_path, preferred_species_lang="de")
    campaigns = [Campaign(name="X", folder=tmp_path / "X", species_filter_mode=FilterMode.LOCATION)]
    worker = AnalysisWorker(runner, FakeCampaignRepo(), proj, campaigns, AnalysisSettings())

    worker.run()

    call = runner.calls[0]
    assert call["preferred_lang"] == "de"
    # The runner no longer receives paths; each campaign folder is both
    # input and output.
    assert "audio_root" not in call
    assert "output_base" not in call
    assert call["campaigns"][0].folder == tmp_path / "X"
