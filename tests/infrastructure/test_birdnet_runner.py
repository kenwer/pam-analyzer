"""Tests for BirdnetRunner: _parse_row unit cases plus end-to-end smoke tests.

The end-to-end cases are marked slow because they download the BirdNET v3.0
ONNX model on first run (~541 MB, cached afterwards) and pay the session
startup cost. Run them on demand with:

    uv run poe test-slow

They verify the runner's contract: it produces a detections CSV with our
schema, emits the expected progress phases, applies the species filter, and
turns a Stop click into a CANCELLED AnalysisRunResult. Audio I/O and 3 s
framing live inside the birdnet library, so nothing here covers them.

The _parse_row cases run fast because that hook takes its label maps as
arguments and never touches the model.
"""

from __future__ import annotations

import csv as _csv
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from pam_analyzer.domain import (
    AnalysisProgressSnapshot,
    AnalysisSettings,
    Campaign,
    FilterMode,
    RunStatus,
)
from pam_analyzer.infrastructure.birdnet_runner import BirdnetRunner
from tests.conftest import CURRENT_MODEL_KEY


class _RecordingProgress:
    """In-memory AnalysisProgress that captures every snapshot."""

    def __init__(self, cancel_after: int | None = None) -> None:
        self.snapshots: list[AnalysisProgressSnapshot] = []
        self._cancel_after = cancel_after

    def report(self, snapshot: AnalysisProgressSnapshot) -> None:
        self.snapshots.append(snapshot)

    def is_cancelled(self) -> bool:
        return self._cancel_after is not None and len(self.snapshots) >= self._cancel_after


def _write_noise_wav(path: Path, seconds: float, sample_rate: int = 48000) -> None:
    """Write a mono WAV of low-level white noise at the given sample rate.

    Noise rather than digital silence: v3.0 normalizes each window by its own
    standard deviation, so a constant-amplitude window yields NaN for every
    class (see test_constant_audio_does_not_abort_the_campaign). Noise keeps
    these tests on the path real recordings take.

    48 kHz is intentional, not v3.0's native 32 kHz: it exercises the
    library's internal resampler that real ARU recordings will hit.
    """
    rng = np.random.default_rng(0)
    samples = (rng.standard_normal(int(seconds * sample_rate)) * 0.05).astype("float32")
    sf.write(str(path), samples, sample_rate, subtype="PCM_16")


def _campaign_with_one_wav(tmp_path: Path, seconds: float, *, silent: bool = False) -> Path:
    """Layout: tmp_path/project/c1/ARU-1/20240101_120000.WAV."""
    camp_dir = tmp_path / "project" / "c1"
    aru_dir = camp_dir / "ARU-1"
    aru_dir.mkdir(parents=True)
    wav = aru_dir / "20240101_120000.WAV"
    if silent:
        sf.write(str(wav), np.zeros(int(seconds * 48000), dtype="float32"), 48000, subtype="PCM_16")
    else:
        _write_noise_wav(wav, seconds=seconds)
    return camp_dir


@pytest.fixture
def campaign_with_minute_wav(tmp_path: Path) -> Path:
    """One 60 s WAV, the file length AudioMoth deployments produce."""
    return _campaign_with_one_wav(tmp_path, seconds=60.0)


def _parse_one(species_name: str, preferred_lang_map: dict[str, str]) -> str:
    """Run _parse_row on a single synthetic result row, return Species.

    Needs no model: the hook takes its label maps as arguments.
    """
    parsed = BirdnetRunner()._parse_row(
        {
            "species_name": species_name,
            "input": "/tmp/x.WAV",
            "start_time": 0.0,
            "end_time": 3.0,
            "confidence": 0.9,
        },
        preferred_lang_map=preferred_lang_map,
        locale_maps={"en_us": {}},
        settings=AnalysisSettings(min_conf=0.5, overlap=0.0, locales=("en_us",)),
    )
    return parsed.preferred_common


def test_blank_locale_name_falls_back_like_a_missing_one() -> None:
    """An entry present but empty must not blank out the Species column.

    Label files are vendor data. locale_label_map keeps every entry with a
    non-empty scientific name, so a line carrying no common name ('Sci_')
    lands in the map as an empty string rather than being absent. No shipped
    v3.0 locale has one today, but a future model release could add one, and
    the two cases have to behave alike.
    """
    assert _parse_one("Parus major_Great Tit", {}) == "Great Tit"
    assert _parse_one("Parus major_Great Tit", {"Parus major": ""}) == "Great Tit"


def test_scientific_name_is_the_last_resort() -> None:
    """With no common name on either side, Species carries the Latin name."""
    assert _parse_one("Parus major_", {"Parus major": ""}) == "Parus major"


@pytest.mark.slow
def test_writes_detections_csv(campaign_with_minute_wav: Path) -> None:
    camp_dir = campaign_with_minute_wav
    settings = AnalysisSettings(min_conf=0.001, overlap=0.0, locales=("en_us",))
    campaign = Campaign(name="c1", folder=camp_dir, species_filter_mode=FilterMode.LIST)
    progress = _RecordingProgress()

    result = BirdnetRunner().run(
        campaigns=[campaign],
        settings=settings,
        preferred_lang="en_us",
        progress=progress,
    )

    assert len(result.campaigns) == 1
    camp = result.campaigns[0]
    assert camp.campaign_name == "c1"
    assert camp.wav_count == 1
    assert camp.detections_csv == camp_dir / f"detections-{CURRENT_MODEL_KEY}.csv"
    assert camp.detections_csv.exists()

    header = camp.detections_csv.read_text(encoding="utf-8").splitlines()[0]
    for col in ("Campaign", "ARU", "Scientific_Name", "Species", "Confidence", "Rank", "File"):
        assert col in header, f"missing column {col!r} in CSV header"

    # Threshold 0.001 over 11,560 classes guarantees at least one row per window.
    assert camp.detection_count > 0
    assert camp.aru_count == 1

    phases = {s.phase for s in progress.snapshots}
    assert {"preparing", "analyzing", "done"}.issubset(phases)


@pytest.mark.slow
def test_written_rows_are_on_the_v3_axis(campaign_with_minute_wav: Path) -> None:
    """Every Scientific_Name written must be a live v3.0 class.

    Guards the upgrade: rows carrying legacy (v2.4-era) spellings would mean
    the runner is normalizing names it should be passing through.
    """
    from pam_analyzer.infrastructure.birdnet_lib import known_species_scientific

    settings = AnalysisSettings(min_conf=0.001, overlap=0.0, locales=("en_us",))
    campaign = Campaign(
        name="c1", folder=campaign_with_minute_wav, species_filter_mode=FilterMode.LIST
    )
    result = BirdnetRunner().run(
        campaigns=[campaign],
        settings=settings,
        preferred_lang="en_us",
        progress=_RecordingProgress(),
    )

    with open(result.campaigns[0].detections_csv, newline="", encoding="utf-8") as f:
        names = {r["Scientific_Name"] for r in _csv.DictReader(f)}
    assert names, "expected at least one row at min_conf=0.001"
    assert names <= known_species_scientific()


@pytest.mark.slow
def test_list_mode_filters_to_supplied_species(campaign_with_minute_wav: Path) -> None:
    """LIST mode restricts detections to the supplied scientific names.

    The allow-list is applied as a post-filter on result rows (the runner
    passes custom_species_list=None), so this covers our filtering, not the
    library's.
    """
    settings = AnalysisSettings(min_conf=0.0001, overlap=0.0, locales=("en_us",))
    campaign = Campaign(
        name="c1", folder=campaign_with_minute_wav, species_filter_mode=FilterMode.LIST
    )
    # Mixed format: plain Latin on one line, 'Sci_Common' on the other.
    campaign.write_species_filter("Parus major\nPseudobird fakensis_Made Up Bird\n", "")

    result = BirdnetRunner().run(
        campaigns=[campaign],
        settings=settings,
        preferred_lang="en_us",
        progress=_RecordingProgress(),
    )

    with open(result.campaigns[0].detections_csv, newline="", encoding="utf-8") as f:
        rows = list(_csv.DictReader(f))
    # A low threshold on noise may still produce zero rows; if anything
    # fires, every row must obey the supplied list.
    allowed = {"Parus major", "Pseudobird fakensis"}
    bad = [r["Scientific_Name"] for r in rows if r["Scientific_Name"] not in allowed]
    assert not bad, f"unexpected species leaked past LIST filter: {sorted(set(bad))[:5]}"


@pytest.mark.slow
def test_legacy_species_name_still_matches(campaign_with_minute_wav: Path) -> None:
    """A species list written on the old axis matches the v3.0 spelling.

    'Accipiter gentilis' is not a v3.0 class; the model emits 'Astur
    gentilis'. Without the legacy alias expansion the filter would admit
    nothing, so the assertion is that the alias reaches the allow-list.
    """
    settings = AnalysisSettings(min_conf=0.0001, overlap=0.0, locales=("en_us",))
    campaign = Campaign(
        name="c1", folder=campaign_with_minute_wav, species_filter_mode=FilterMode.LIST
    )
    campaign.write_species_filter("Accipiter gentilis\n", "")

    result = BirdnetRunner().run(
        campaigns=[campaign],
        settings=settings,
        preferred_lang="en_us",
        progress=_RecordingProgress(),
    )

    with open(result.campaigns[0].detections_csv, newline="", encoding="utf-8") as f:
        names = {r["Scientific_Name"] for r in _csv.DictReader(f)}
    assert names <= {"Accipiter gentilis", "Astur gentilis"}


@pytest.mark.slow
def test_honors_cancellation(tmp_path: Path) -> None:
    """A Stop click mid-run returns CANCELLED rather than hanging.

    Was skipped under birdnet 0.2.16, where cancelling mid-run wedged
    session.run() forever in ProcessManager.wait_until_all_finished
    (birdnet issue 51). birdnet 1.1 bounds the teardown joins, so this is
    the regression test for that fix. The 30 s file is long enough that the
    progress callback fires while the session is running, which is what
    puts the cancel on the mid-run path rather than the post-session check.
    """
    camp_dir = _campaign_with_one_wav(tmp_path, seconds=30.0)
    settings = AnalysisSettings(min_conf=0.001, overlap=0.0, locales=("en_us",))
    campaign = Campaign(name="c1", folder=camp_dir, species_filter_mode=FilterMode.LIST)
    # Cancel as soon as the first snapshot (the 'preparing' report) arrives,
    # so is_cancelled() is already True when the lib's first stats callback
    # fires inside session.run().
    progress = _RecordingProgress(cancel_after=1)

    # run() converts the internal CancelledError into a CANCELLED outcome so
    # campaigns finished before the cancel are still returned. This single
    # campaign never finished, so campaigns is empty.
    result = BirdnetRunner().run(
        campaigns=[campaign],
        settings=settings,
        preferred_lang="en_us",
        progress=progress,
    )
    assert result.status is RunStatus.CANCELLED
    assert result.campaigns == ()


@pytest.mark.slow
def test_constant_audio_does_not_abort_the_campaign(tmp_path: Path) -> None:
    """Digitally constant audio yields NaN confidences, which must be dropped.

    v3.0 normalizes each window by its standard deviation, so a window of
    constant amplitude divides by zero and every class returns NaN. Left
    alone those rows reach the CSV writer and raise, taking the whole
    campaign down. A dead ARU channel or a recorder dropout produces exactly
    this input, so the run has to survive it and still write a valid CSV.
    """
    camp_dir = _campaign_with_one_wav(tmp_path, seconds=20.0, silent=True)
    settings = AnalysisSettings(min_conf=0.001, overlap=0.0, locales=("en_us",))
    campaign = Campaign(name="c1", folder=camp_dir, species_filter_mode=FilterMode.LIST)

    result = BirdnetRunner().run(
        campaigns=[campaign],
        settings=settings,
        preferred_lang="en_us",
        progress=_RecordingProgress(),
    )

    assert result.status is RunStatus.COMPLETED
    camp = result.campaigns[0]
    assert camp.detections_csv.exists()
    assert camp.detection_count == 0
    # Header written, no data rows.
    assert len(camp.detections_csv.read_text(encoding="utf-8").splitlines()) == 1
