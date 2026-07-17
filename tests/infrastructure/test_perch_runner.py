"""Smoke test for PerchRunner end-to-end on synthetic silent WAVs.

Marked slow because it downloads the Perch v2 SavedModel via kagglehub on
first run (cached afterwards) and pays the TensorFlow load cost (~10 s).
Run on demand with:

    uv run poe test -m slow

The audio I/O and 5 s framing that used to live in this module now sit
inside the birdnet>=0.2 library, so the unit tests against
_frame_into_windows are gone. The remaining cases verify the runner's
contract: it produces a detections CSV with our schema, emits the expected
progress phases, and translates a Stop click into a CancelledError.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from pam_analyzer.domain import (
    AnalysisProgressSnapshot,
    AnalysisSettings,
    CampaignRunInput,
    CancelledError,
    FilterMode,
)
from pam_analyzer.infrastructure.perch_runner import PerchRunner


class _RecordingProgress:
    """In-memory AnalysisProgress that captures every snapshot."""

    def __init__(self, cancel_after: int | None = None) -> None:
        self.snapshots: list[AnalysisProgressSnapshot] = []
        self._cancel_after = cancel_after

    def report(self, snapshot: AnalysisProgressSnapshot) -> None:
        self.snapshots.append(snapshot)

    def is_cancelled(self) -> bool:
        return self._cancel_after is not None and len(self.snapshots) >= self._cancel_after


def _write_silent_wav(path: Path, seconds: float, sample_rate: int = 48000) -> None:
    """Write a short mono WAV of zeros at the given sample rate.

    48 kHz is intentional, not Perch's native 32 kHz: it exercises the
    library's internal resampler that real ARU recordings will hit.
    """
    n = int(seconds * sample_rate)
    samples = np.zeros(n, dtype="float32")
    sf.write(str(path), samples, sample_rate, subtype="PCM_16")


def _campaign_with_one_wav(tmp_path: Path, seconds: float) -> Path:
    """Layout: tmp_path/project/c1/ARU-1/20240101_120000.WAV (silent)."""
    camp_dir = tmp_path / "project" / "c1"
    aru_dir = camp_dir / "ARU-1"
    aru_dir.mkdir(parents=True)
    _write_silent_wav(aru_dir / "20240101_120000.WAV", seconds=seconds)
    return camp_dir


@pytest.fixture
def campaign_with_minute_wav(tmp_path: Path) -> Path:
    """One 60 s WAV, the file length AudioMoth deployments produce.

    Runs this long reach a steady state before winding down. Near-instant
    runs can race birdnet's pipeline completion and hang session.run()
    forever in ProcessManager.wait_until_all_finished (a session.cancel()
    variant of the same hang is birdnet issue 51).
    """
    return _campaign_with_one_wav(tmp_path, seconds=60.0)


@pytest.fixture
def campaign_with_short_wav(tmp_path: Path) -> Path:
    """One 6 s WAV, deliberately short, for the cancellation test only.

    On a fast machine the run finishes before the cancel takes effect and
    CancelledError comes from the runner's post-session check. On slower
    machines the cancel lands mid-run and session.run() hangs forever
    (birdnet issue 51), which is why the test is currently skipped. If the
    upstream fix lands and the skip is removed, do not hand this test a
    longer file: that guarantees a mid-run cancel on every machine.
    """
    return _campaign_with_one_wav(tmp_path, seconds=6.0)


@pytest.mark.slow
def test_perch_runner_writes_detections_csv_for_silent_input(
    campaign_with_minute_wav: Path, tmp_path: Path
) -> None:
    camp_dir = campaign_with_minute_wav
    settings = AnalysisSettings(min_conf=0.001, overlap=0.0, locales=("en_us",))
    ci = CampaignRunInput(
        name="c1",
        folder=camp_dir,
        mode=FilterMode.LIST,
        location=None,
        species_list_text=None,
    )
    progress = _RecordingProgress()

    result = PerchRunner().run(
        campaigns=[ci],
        settings=settings,
        preferred_lang="en_us",
        progress=progress,
    )

    assert len(result.campaigns) == 1
    camp = result.campaigns[0]
    assert camp.campaign_name == "c1"
    assert camp.wav_count == 1
    assert camp.detections_csv == camp_dir / "detections-Perch-2.0.csv"
    assert camp.detections_csv.exists()

    header = camp.detections_csv.read_text(encoding="utf-8").splitlines()[0]
    for col in ("Campaign", "ARU", "Scientific_Name", "Species", "Confidence", "Rank", "File"):
        assert col in header, f"missing column {col!r} in CSV header"

    # Threshold 0.001 over 14795 classes guarantees at least one row per window.
    assert camp.detection_count > 0
    assert camp.aru_count == 1

    phases = {s.phase for s in progress.snapshots}
    assert {"preparing", "analyzing", "done"}.issubset(phases)


@pytest.mark.slow
def test_perch_runner_list_mode_filters_to_supplied_species(
    campaign_with_minute_wav: Path, tmp_path: Path
) -> None:
    """LIST mode restricts detections to the supplied scientific names.

    The library accepts our parsed species set as custom_species_list, so
    even though Perch's class axis is 14,795 wide, the lib only emits rows
    for species in the set.
    """
    camp_dir = campaign_with_minute_wav
    settings = AnalysisSettings(min_conf=0.0001, overlap=0.0, locales=("en_us",))
    ci = CampaignRunInput(
        name="c1",
        folder=camp_dir,
        mode=FilterMode.LIST,
        location=None,
        # Mixed format: plain Latin on one line, 'Sci_Common' on the other.
        species_list_text="Parus major\nPseudobird fakensis_Made Up Bird\n",
    )
    progress = _RecordingProgress()

    result = PerchRunner().run(
        campaigns=[ci],
        settings=settings,
        preferred_lang="en_us",
        progress=progress,
    )

    csv_path = result.campaigns[0].detections_csv
    import csv as _csv

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(_csv.DictReader(f))
    # Filter may legitimately produce zero rows for silent input; if the
    # threshold is low enough that anything fires, every row must obey the
    # supplied list.
    allowed = {"Parus major", "Pseudobird fakensis"}
    bad = [r["Scientific_Name"] for r in rows if r["Scientific_Name"] not in allowed]
    assert not bad, f"unexpected species leaked past LIST filter: {sorted(set(bad))[:5]}"


@pytest.mark.slow
@pytest.mark.skip(
    reason="a cancel landing mid-run hangs session.run() forever in "
    "ProcessManager.wait_until_all_finished; hit on all four CI platforms; "
    "see https://github.com/birdnet-team/birdnet/issues/51",
)
def test_perch_runner_honors_cancellation(
    campaign_with_short_wav: Path, tmp_path: Path
) -> None:
    camp_dir = campaign_with_short_wav
    settings = AnalysisSettings(min_conf=0.001, overlap=0.0, locales=("en_us",))
    ci = CampaignRunInput(
        name="c1",
        folder=camp_dir,
        mode=FilterMode.LIST,
        location=None,
        species_list_text=None,
    )
    # Cancel as soon as the first snapshot (the 'preparing' report) arrives.
    progress = _RecordingProgress(cancel_after=1)

    with pytest.raises(CancelledError):
        PerchRunner().run(
            campaigns=[ci],
            settings=settings,
            preferred_lang="en_us",
            progress=progress,
        )
