"""Smoke test for PerchRunner end-to-end on a tiny synthetic WAV.

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

import sys
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


@pytest.fixture
def campaign_with_one_wav(tmp_path: Path) -> tuple[Path, Path]:
    """Layout: tmp_path/audio/c1/ARU-1/20240101_120000.WAV (6 s, silent)."""
    audio_root = tmp_path / "audio"
    camp_dir = audio_root / "c1"
    aru_dir = camp_dir / "ARU-1"
    aru_dir.mkdir(parents=True)
    _write_silent_wav(aru_dir / "20240101_120000.WAV", seconds=6.0)
    return audio_root, camp_dir


@pytest.mark.slow
def test_perch_runner_writes_detections_csv_for_silent_input(
    campaign_with_one_wav: tuple[Path, Path], tmp_path: Path
) -> None:
    audio_root, camp_dir = campaign_with_one_wav
    out_base = tmp_path / "out"
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
        output_base=out_base,
        settings=settings,
        preferred_lang="en_us",
        audio_root=audio_root,
        progress=progress,
    )

    assert len(result.campaigns) == 1
    camp = result.campaigns[0]
    assert camp.campaign_name == "c1"
    assert camp.wav_count == 1
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
    campaign_with_one_wav: tuple[Path, Path], tmp_path: Path
) -> None:
    """LIST mode restricts detections to the supplied scientific names.

    The library accepts our parsed species set as custom_species_list, so
    even though Perch's class axis is 14,795 wide, the lib only emits rows
    for species in the set.
    """
    audio_root, camp_dir = campaign_with_one_wav
    out_base = tmp_path / "out"
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
        output_base=out_base,
        settings=settings,
        preferred_lang="en_us",
        audio_root=audio_root,
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
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="birdnet's session teardown after cancel() can hang forever in "
    "ProcessManager.join(), which deadlocked Windows CI; see "
    "https://github.com/birdnet-team/birdnet/issues/51",
)
def test_perch_runner_honors_cancellation(
    campaign_with_one_wav: tuple[Path, Path], tmp_path: Path
) -> None:
    audio_root, camp_dir = campaign_with_one_wav
    out_base = tmp_path / "out"
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
            output_base=out_base,
            settings=settings,
            preferred_lang="en_us",
            audio_root=audio_root,
            progress=progress,
        )
