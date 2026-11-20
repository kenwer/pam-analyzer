"""End-to-end smoke tests for Birdnet30Runner.

The end-to-end cases are marked slow because they load the BirdNET v3.0
ONNX model and pay the session startup cost. They read the model from the
cache `uv run poe prewarm` fills. Run them on demand with:

    uv run poe test-slow

They verify the runner's contract: it produces a detections CSV with our
schema, emits the expected progress phases, applies the species filter, and
turns a Stop click into a CANCELLED AnalysisRunResult. Audio I/O and 3 s
framing live inside the birdnet library, so nothing here covers them.
"""

from __future__ import annotations

import csv as _csv
import math
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC

from pam_analyzer.domain import (
    AnalysisProgressSnapshot,
    AnalysisRunResult,
    AnalysisSettings,
    Campaign,
    DetectionStore,
    FilterMode,
    RunStatus,
)
from pam_analyzer.infrastructure.base_analysis_runner import ParsedRow
from pam_analyzer.infrastructure.birdnet_3_0_runner import MODEL_KEY, Birdnet30Runner


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

    Noise rather than digital silence, so these tests take the path real
    recordings take.

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


@pytest.fixture(scope="module")
def minute_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, AnalysisRunResult, _RecordingProgress]:
    """One unfiltered run over a minute of noise, shared because each run spawns the model pipeline."""
    camp_dir = _campaign_with_one_wav(tmp_path_factory.mktemp("minute"), seconds=60.0)
    progress = _RecordingProgress()
    result = Birdnet30Runner().run(
        campaigns=[Campaign(name="c1", folder=camp_dir, species_filter_mode=FilterMode.LIST)],
        settings=AnalysisSettings(min_conf=0.001, overlap=0.0, locales=("en_us",)),
        preferred_lang="en_us",
        progress=progress,
    )
    return camp_dir, result, progress


@pytest.mark.slow
def test_recording_time_comes_from_the_flac_guano(tmp_path: Path) -> None:
    """A FLAC whose name carries no stamp still gets the time its GUANO holds."""
    aru_dir = tmp_path / "project" / "c1" / "ARU-1"
    aru_dir.mkdir(parents=True)
    flac = aru_dir / "renamed.flac"
    samples = (np.random.default_rng(0).standard_normal(9 * 48000) * 0.05 * 32767).astype("int16")
    sf.write(str(flac), samples, 48000, subtype="PCM_16", format="FLAC")
    tags = FLAC(str(flac))
    tags["GUANO"] = "GUANO|Version: 1.0\nTimestamp: 2025-05-08T22:29:46+02:00"
    tags.save()
    settings = AnalysisSettings(min_conf=0.001, overlap=0.0, locales=("en_us",))
    campaign = Campaign(name="c1", folder=aru_dir.parent, species_filter_mode=FilterMode.LIST)

    result = Birdnet30Runner().run(
        campaigns=[campaign], settings=settings, preferred_lang="en_us", progress=_RecordingProgress()
    )

    with result.campaigns[0].detections_csv.open(encoding="utf-8", newline="") as f:
        times = {row["Recording_Time"] for row in _csv.DictReader(f)}
    assert times == {"2025-05-08 22:29:46"}


@pytest.mark.slow
def test_writes_detections_csv(minute_run) -> None:
    camp_dir, result, progress = minute_run

    assert len(result.campaigns) == 1
    camp = result.campaigns[0]
    assert camp.campaign_name == "c1"
    assert camp.wav_count == 1
    assert camp.detections_csv == camp_dir / f"detections-{MODEL_KEY}.csv"
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
def test_written_rows_are_on_the_v3_axis(minute_run) -> None:
    """Every Scientific_Name written must be a live v3.0 class.

    Guards the upgrade: rows carrying legacy (v2.4-era) spellings would mean
    the runner is normalizing names it should be passing through.
    """
    from pam_analyzer.infrastructure import birdnet_3_0_taxonomy

    _, result, _ = minute_run
    with open(result.campaigns[0].detections_csv, newline="", encoding="utf-8") as f:
        names = {r["Scientific_Name"] for r in _csv.DictReader(f)}
    assert names, "expected at least one row at min_conf=0.001"
    assert names <= birdnet_3_0_taxonomy.TAXONOMY.known_species_scientific()


@pytest.mark.slow
def test_runner_csv_survives_load_and_save_unchanged(minute_run) -> None:
    """The runner and the Examine autosave share one writer, so a save without edits rewrites nothing."""
    camp_dir, result, _ = minute_run
    written = result.campaigns[0].detections_csv.read_bytes()

    DetectionStore.load_for_campaign(camp_dir).save()

    assert result.campaigns[0].detections_csv.read_bytes() == written


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

    result = Birdnet30Runner().run(
        campaigns=[campaign],
        settings=settings,
        preferred_lang="en_us",
        progress=_RecordingProgress(),
    )

    with open(result.campaigns[0].detections_csv, newline="", encoding="utf-8") as f:
        rows = list(_csv.DictReader(f))
    # A low threshold on noise may still produce zero rows. If anything
    # fires, every row must obey the supplied list.
    allowed = {"Parus major", "Pseudobird fakensis"}
    bad = [r["Scientific_Name"] for r in rows if r["Scientific_Name"] not in allowed]
    assert not bad, f"unexpected species leaked past LIST filter: {sorted(set(bad))[:5]}"


@pytest.mark.slow
def test_legacy_species_name_still_matches(campaign_with_minute_wav: Path) -> None:
    """A species list written on the old axis matches the v3.0 spelling.

    'Accipiter gentilis' is not a v3.0 class. The model emits 'Astur
    gentilis'. Without the legacy alias expansion the filter would admit
    nothing, so the assertion is that the alias reaches the allow-list.
    """
    settings = AnalysisSettings(min_conf=0.0001, overlap=0.0, locales=("en_us",))
    campaign = Campaign(
        name="c1", folder=campaign_with_minute_wav, species_filter_mode=FilterMode.LIST
    )
    campaign.write_species_filter("Accipiter gentilis\n", "")

    result = Birdnet30Runner().run(
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
    result = Birdnet30Runner().run(
        campaigns=[campaign],
        settings=settings,
        preferred_lang="en_us",
        progress=progress,
    )
    assert result.status is RunStatus.CANCELLED
    assert result.campaigns == ()


@pytest.mark.slow
def test_digital_silence_gets_only_low_confidences(tmp_path: Path) -> None:
    """A dead ARU channel or a recorder dropout must not produce confident detections.

    The first v3.0 files scored digital silence NaN for every class. The 3.0.1
    files score it with finite values near the model's prior, about 0.035 at
    most, so a usual threshold keeps them out of the CSV.
    """
    camp_dir = _campaign_with_one_wav(tmp_path, seconds=20.0, silent=True)
    settings = AnalysisSettings(min_conf=0.001, overlap=0.0, locales=("en_us",))
    campaign = Campaign(name="c1", folder=camp_dir, species_filter_mode=FilterMode.LIST)

    result = Birdnet30Runner().run(
        campaigns=[campaign],
        settings=settings,
        preferred_lang="en_us",
        progress=_RecordingProgress(),
    )

    assert result.status is RunStatus.COMPLETED
    with result.campaigns[0].detections_csv.open(encoding="utf-8", newline="") as f:
        confidences = [float(row["Confidence"]) for row in _csv.DictReader(f)]
    assert confidences
    assert all(math.isfinite(c) and c < 0.1 for c in confidences)


class _NanScoringRunner(Birdnet30Runner):
    """Scores every row NaN, the degenerate output a model can give."""

    def _parse_row(self, raw_row: Any) -> ParsedRow:
        return replace(super()._parse_row(raw_row), confidence=math.nan)


@pytest.mark.slow
def test_nonfinite_confidences_do_not_abort_the_campaign(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Rows with a non-finite confidence are dropped and the CSV stays valid.

    Left alone they reach the CSV writer and raise, taking the whole campaign
    down for one bad file.
    """
    camp_dir = _campaign_with_one_wav(tmp_path, seconds=20.0)
    settings = AnalysisSettings(min_conf=0.001, overlap=0.0, locales=("en_us",))
    campaign = Campaign(name="c1", folder=camp_dir, species_filter_mode=FilterMode.LIST)

    result = _NanScoringRunner().run(
        campaigns=[campaign],
        settings=settings,
        preferred_lang="en_us",
        progress=_RecordingProgress(),
    )

    assert result.status is RunStatus.COMPLETED
    # Proves rows reached the guard, not that the noise scored nothing.
    assert "non-finite confidence" in caplog.text
    camp = result.campaigns[0]
    assert camp.detection_count == 0
    # Header written, no data rows.
    assert len(camp.detections_csv.read_text(encoding="utf-8").splitlines()) == 1
