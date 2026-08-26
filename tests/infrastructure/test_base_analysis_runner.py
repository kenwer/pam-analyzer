"""The run() loop returns completed campaigns no matter how the run ends.

These exercise only the campaign-sequencing loop in BaseAnalysisRunner, so
they stub _run_campaign and the model hooks rather than loading a real model.
The point under test is that a cancel or a mid-batch failure returns the
campaigns that already finished (their CSVs are on disk) tagged with the
outcome, instead of discarding them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pam_analyzer.domain import (
    AnalysisProgressSnapshot,
    AnalysisSettings,
    Campaign,
    FilterMode,
    RunStatus,
)
from pam_analyzer.domain.analysis_run_result import CampaignRunResult
from pam_analyzer.infrastructure.base_analysis_runner import BaseAnalysisRunner, _SegmentRanker


class _CountingProgress:
    """AnalysisProgress whose is_cancelled() flips True after N queries.

    _run_campaign is stubbed here, so the only is_cancelled() caller is the
    top-of-loop check, one per campaign. cancel_after=2 therefore lets two
    campaigns run, then cancels the third.
    """

    def __init__(self, cancel_after: int | None = None) -> None:
        self._cancel_after = cancel_after
        self._queries = 0

    def report(self, snapshot: AnalysisProgressSnapshot) -> None:
        pass

    def is_cancelled(self) -> bool:
        self._queries += 1
        return self._cancel_after is not None and self._queries > self._cancel_after


class _StubRunner(BaseAnalysisRunner):
    """Concrete runner that records which campaigns ran and can fail on cue.

    Implements the model hooks with stubs so the loop under test runs
    without ONNX or any model download.
    """

    model_key = "Stub-1.0"
    log_prefix = "stub"

    def __init__(self, fail_on: str | None = None) -> None:
        self.ran: list[str] = []
        self._fail_on = fail_on

    def _load_model(self) -> Any:
        return object()

    def _open_predict_session(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("_run_campaign is stubbed, so no session is opened")

    def _parse_row(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("_run_campaign is stubbed, so no rows are parsed")

    def _run_campaign(self, campaign: Campaign, *_a: Any, **_k: Any) -> CampaignRunResult:
        if campaign.name == self._fail_on:
            raise RuntimeError(f"boom in {campaign.name}")
        self.ran.append(campaign.name)
        return CampaignRunResult(
            campaign_name=campaign.name,
            detections_csv=campaign.folder / "detections-Stub-1.0.csv",
            detection_count=0,
            wav_count=0,
            aru_count=0,
            elapsed=0.0,
        )


def _campaigns(tmp_path: Path, names: list[str]) -> list[Campaign]:
    out = []
    for name in names:
        folder = tmp_path / name
        folder.mkdir()
        out.append(Campaign(name=name, folder=folder, species_filter_mode=FilterMode.LIST))
    return out


def _run(runner: _StubRunner, campaigns: list[Campaign], progress: _CountingProgress):
    return runner.run(
        campaigns=campaigns,
        settings=AnalysisSettings(),
        preferred_lang="en_us",
        progress=progress,
    )


def test_full_run_reports_completed(tmp_path: Path) -> None:
    runner = _StubRunner()
    result = _run(runner, _campaigns(tmp_path, ["a", "b", "c"]), _CountingProgress())

    assert result.status is RunStatus.COMPLETED
    assert [c.campaign_name for c in result.campaigns] == ["a", "b", "c"]
    assert result.error is None


def test_cancel_keeps_completed_campaigns(tmp_path: Path) -> None:
    runner = _StubRunner()
    result = _run(
        runner, _campaigns(tmp_path, ["a", "b", "c"]), _CountingProgress(cancel_after=2)
    )

    assert result.status is RunStatus.CANCELLED
    # a and b finished before the cancel; c was never started.
    assert [c.campaign_name for c in result.campaigns] == ["a", "b"]
    assert runner.ran == ["a", "b"]


def test_failure_keeps_earlier_campaigns_and_carries_message(tmp_path: Path) -> None:
    runner = _StubRunner(fail_on="b")
    result = _run(runner, _campaigns(tmp_path, ["a", "b", "c"]), _CountingProgress())

    assert result.status is RunStatus.FAILED
    # a completed before b blew up; c after the failure never ran.
    assert [c.campaign_name for c in result.campaigns] == ["a"]
    assert result.error is not None
    assert "boom in b" in result.error


def test_ranks_count_up_within_one_segment() -> None:
    ranker = _SegmentRanker()
    seg = ("a.flac", 0.0)

    assert ranker.rank_for(seg, "Turdus merula") == 1
    assert ranker.rank_for(seg, "Parus major") == 2


def test_ranks_restart_on_a_new_segment() -> None:
    ranker = _SegmentRanker()

    assert ranker.rank_for(("a.flac", 0.0), "Turdus merula") == 1
    assert ranker.rank_for(("a.flac", 3.0), "Parus major") == 1


def test_a_species_already_ranked_in_this_segment_is_refused() -> None:
    """v3.0 carries Charadrius dubius and Thinornis dubius as separate
    classes for one bird. Both canonicalise to Thinornis dubius, so the
    second one to arrive must not produce a row.
    """
    ranker = _SegmentRanker()
    seg = ("a.flac", 0.0)

    assert ranker.rank_for(seg, "Thinornis dubius") == 1
    assert ranker.rank_for(seg, "Thinornis dubius") is None


def test_ranks_close_up_after_a_refusal() -> None:
    """The refused row must not consume a rank, or the CSV shows a gap."""
    ranker = _SegmentRanker()
    seg = ("a.flac", 0.0)

    assert ranker.rank_for(seg, "Thinornis dubius") == 1
    assert ranker.rank_for(seg, "Thinornis dubius") is None
    assert ranker.rank_for(seg, "Turdus merula") == 2


def test_the_same_species_ranks_again_in_the_next_segment() -> None:
    ranker = _SegmentRanker()

    assert ranker.rank_for(("a.flac", 0.0), "Thinornis dubius") == 1
    assert ranker.rank_for(("a.flac", 3.0), "Thinornis dubius") == 1
