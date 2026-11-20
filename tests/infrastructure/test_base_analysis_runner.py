"""The run() loop returns completed campaigns no matter how the run ends.

These exercise only the campaign-sequencing loop in BaseAnalysisRunner, so
they stub _run_campaign and the model hooks rather than loading a real model.
The point under test is that a cancel or a mid-batch failure returns the
campaigns that already finished (their CSVs are on disk) tagged with the
outcome, instead of discarding them.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from pam_analyzer.domain import (
    AnalysisProgressSnapshot,
    AnalysisSettings,
    Campaign,
    FilterMode,
    ModelInfo,
    RunStatus,
)
from pam_analyzer.domain.analysis_run_result import CampaignRunResult
from pam_analyzer.infrastructure.base_analysis_runner import BaseAnalysisRunner, _common_names, _SegmentRanker
from pam_analyzer.infrastructure.http_download import DownloadError
from pam_analyzer.infrastructure.model_store import Artifact, ModelPackage
from tests.infrastructure.conftest import Route

_STUB_INFO = ModelInfo("Stub 1.0", "https://example.org", "CC0")


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

    def __init__(self, fail_on: str | None = None, artifacts: tuple[Artifact, ...] = ()) -> None:
        self.ran: list[str] = []
        self.events: list[str] = []
        self._fail_on = fail_on
        self.loaded = False
        self.package = ModelPackage(_STUB_INFO, lambda: artifacts, lambda: self.events.append("prepare"))

    def _load_model(self) -> Any:
        self.loaded = True
        self.events.append("load")
        return object()

    def _open_predict_session(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("_run_campaign is stubbed, so no session is opened")

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
    # a and b finished before the cancel, c was never started.
    assert [c.campaign_name for c in result.campaigns] == ["a", "b"]
    assert runner.ran == ["a", "b"]


def test_failure_keeps_earlier_campaigns_and_carries_message(tmp_path: Path) -> None:
    runner = _StubRunner(fail_on="b")
    result = _run(runner, _campaigns(tmp_path, ["a", "b", "c"]), _CountingProgress())

    assert result.status is RunStatus.FAILED
    # a completed before b blew up, c after the failure never ran.
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


_WEIGHTS = bytes(range(256)) * 8_000


def _served_artifact(server, tmp_path: Path, path: str = "/w.onnx") -> Artifact:
    server.routes[path] = Route(_WEIGHTS)
    return Artifact(
        id="stub-weights",
        url=server.url(path),
        size=len(_WEIGHTS),
        sha256=hashlib.sha256(_WEIGHTS).hexdigest(),
        dest=tmp_path / "models" / "w.onnx",
    )


def test_install_model_downloads_and_prepares_without_loading(tmp_path: Path, local_http_server) -> None:
    artifact = _served_artifact(local_http_server, tmp_path)
    runner = _StubRunner(artifacts=(artifact,))
    assert runner.missing_download_bytes() == len(_WEIGHTS)
    calls: list[tuple[int, int]] = []
    runner.install_model(lambda done, total: calls.append((done, total)), lambda: False)
    assert runner.missing_download_bytes() == 0
    assert calls[-1] == (len(_WEIGHTS), len(_WEIGHTS))
    assert runner.events == ["prepare"]


def test_failed_install_names_the_engine(tmp_path: Path, local_http_server) -> None:
    artifact = _served_artifact(local_http_server, tmp_path)
    local_http_server.routes["/w.onnx"] = Route(b"", status=503)
    with pytest.raises(DownloadError, match=r"^Downloading Stub 1\.0 failed: .*503.*\. Check the internet"):
        _StubRunner(artifacts=(artifact,)).install_model(lambda *_: None, lambda: False)


def test_run_does_not_download(tmp_path: Path, local_http_server) -> None:
    """The worker installs the model first, so run() goes straight to loading it."""
    runner = _StubRunner(artifacts=(_served_artifact(local_http_server, tmp_path),))
    result = _run(runner, _campaigns(tmp_path, ["a"]), _CountingProgress())
    assert result.status is RunStatus.COMPLETED
    assert runner.events == ["load"]
    assert local_http_server.requests == []


def test_the_default_parse_splits_birdnets_label_format() -> None:
    row = {"species_name": "Parus major_Great Tit", "input": "x.WAV", "start_time": 0.0, "end_time": 3.0, "confidence": 0.9}

    parsed = _StubRunner()._parse_row(row)

    assert (parsed.label, parsed.emitted_en) == ("Parus major", "Great Tit")


def test_blank_locale_name_falls_back_like_a_missing_one() -> None:
    """An entry present but empty must not blank out the Species column.

    Label files are vendor data. locale_label_map keeps every entry with a
    non-empty scientific name, so a line carrying no common name ('Sci_')
    lands in the map as an empty string rather than being absent.
    """
    assert _common_names("Parus major", "Great Tit", {}, {})[0] == "Great Tit"
    assert _common_names("Parus major", "Great Tit", {"Parus major": ""}, {})[0] == "Great Tit"


def test_scientific_name_is_the_last_resort() -> None:
    assert _common_names("Parus major", "", {"Parus major": ""}, {})[0] == "Parus major"


def test_english_column_comes_from_the_label_map() -> None:
    """The map carries the reference taxonomy's name. The name a model prints
    with a lumped class names the species as that model drew it.
    """
    _, commons = _common_names("Acanthis flammea", "Hoary Redpoll", {}, {"en_us": {"Acanthis flammea": "Redpoll"}})

    assert commons == {"en_us": "Redpoll"}


def test_english_column_falls_back_to_the_emitted_name() -> None:
    _, commons = _common_names("Turdus merula", "Eurasian Blackbird", {}, {"en_us": {}, "de": {}})

    assert commons == {"en_us": "Eurasian Blackbird", "de": ""}


class _FakeTaxonomy:
    """The three taxonomy lookups _run_campaign makes, without any label download."""

    def known_species_scientific(self) -> frozenset[str]:
        return frozenset({"Parus major", "Turdus merula"})

    def region_species_scientific(self, lat: float, lon: float, week: int) -> frozenset[str]:  # pragma: no cover
        raise AssertionError("LIST mode does not consult the geo model")

    def locale_label_map(self, lang: str) -> dict[str, str]:
        return {}


class _FakeSession:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def run(self, files: list[Path]) -> _FakeSession:
        return self

    def to_structured_array(self) -> list[dict[str, Any]]:
        return self._rows


class _RowLoopRunner(BaseAnalysisRunner):
    """Runs the real _run_campaign row loop over canned result rows."""

    model_key = "Stub-1.0"
    log_prefix = "stub"
    taxonomy = _FakeTaxonomy()  # type: ignore[assignment]
    package = ModelPackage(_STUB_INFO, lambda: (), lambda: None)

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.known_species_builds = 0

    def _load_model(self) -> Any:
        return object()

    def _open_predict_session(self, *args: Any, **kwargs: Any) -> Any:
        return _FakeSession(self._rows)

    def _known_output_species(self):
        self.known_species_builds += 1
        return super()._known_output_species()


def _known_species_builds(tmp_path: Path, dropped_rows: int) -> int:
    tmp_path.mkdir()
    campaign = _campaigns(tmp_path, ["c1"])[0]
    audio = campaign.folder / "aru1" / "20260301_060000.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"")
    campaign.write_species_filter("Parus major\n", "")
    row = {"input": str(audio), "end_time": 3.0, "confidence": 0.5, "species_name": "Turdus merula_Eurasian Blackbird"}
    runner = _RowLoopRunner([{**row, "start_time": float(i)} for i in range(dropped_rows)])

    result = _run(runner, [campaign], _CountingProgress())

    assert result.status is RunStatus.COMPLETED
    assert result.campaigns[0].detection_count == 0
    return runner.known_species_builds


def test_known_species_builds_do_not_grow_with_dropped_rows(tmp_path: Path) -> None:
    """Rebuilding the canonical axis per dropped row cost minutes on a large campaign."""
    assert _known_species_builds(tmp_path / "one", 1) == _known_species_builds(tmp_path / "many", 50)
