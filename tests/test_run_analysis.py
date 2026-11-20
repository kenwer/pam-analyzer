"""scripts/run_analysis.py selects campaigns by the names typed on the command line and exits with the run's outcome."""

import unicodedata
from pathlib import Path

import pytest

from pam_analyzer.domain import AnalysisRunResult, Campaign, Project, RunStatus
from scripts import run_analysis
from scripts.run_analysis import _select_campaigns


def test_a_campaign_name_matches_in_either_unicode_form(tmp_path: Path) -> None:
    """macOS hands folder names back decomposed (NFD), while a typed argument is composed (NFC)."""
    folder = tmp_path / unicodedata.normalize("NFD", "TüCamp1")
    folder.mkdir()
    Campaign(name=folder.name, folder=folder).save()

    selected = _select_campaigns(tmp_path, [unicodedata.normalize("NFC", "TüCamp1")])

    assert selected is not None
    assert [c.folder for c in selected] == [folder]


class _EndingRunner:
    """Stands in for BirdnetRunner and ends every run with one status."""

    model_key = "Stub-1.0"
    status = RunStatus.COMPLETED

    def run(self, **_kwargs: object) -> AnalysisRunResult:
        return AnalysisRunResult(status=self.status, error="boom" if self.status is RunStatus.FAILED else None)


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [(RunStatus.COMPLETED, 0), (RunStatus.CANCELLED, 130), (RunStatus.FAILED, 1)],
)
def test_exit_code_follows_how_the_run_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: RunStatus, exit_code: int
) -> None:
    """The runner reports a cancel or a failure in its result, never by raising."""
    Project.create(tmp_path)
    (tmp_path / "c1").mkdir()
    Campaign(name="c1", folder=tmp_path / "c1").save()
    monkeypatch.setattr(_EndingRunner, "status", status)
    monkeypatch.setattr(run_analysis, "BirdnetRunner", _EndingRunner)

    assert run_analysis.main(["--project", str(tmp_path)]) == exit_code
