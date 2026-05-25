"""Smoke test for PerchRunner end-to-end on a tiny synthetic WAV.

Marked slow because it downloads the Perch v2 SavedModel via kagglehub on
first run (cached afterwards) and pays the TensorFlow import + saved_model
load cost (~10 s). Run on demand with:

    uv run poe test -m slow

The test does not assert on model output. It only verifies the wiring:
that the runner produces a detections CSV with the expected header, that
progress snapshots reach the 'done' phase, and that cancellation is
honored mid-run.
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
from pam_analyzer.infrastructure.perch_runner import (
    PERCH_SAMPLE_RATE,
    PERCH_WINDOW_SECONDS,
    PerchRunner,
    _frame_into_windows,
)


def test_frame_into_windows_zero_overlap_tiles_one_per_5s_block() -> None:
    samples = np.zeros(int(PERCH_SAMPLE_RATE * 12.0), dtype="float32")
    windows = _frame_into_windows(samples, overlap_seconds=0.0)
    # 12 s of audio with 5 s windows -> ceil(12/5) = 3 windows (last padded).
    assert windows.shape == (3, int(PERCH_SAMPLE_RATE * PERCH_WINDOW_SECONDS))


def test_frame_into_windows_with_overlap_yields_more_windows() -> None:
    """Overlap halving the stride should roughly double the window count.

    12 s of audio at 2.5 s stride covers all real data with 4 windows
    (starts: 0, 2.5, 5, 7.5; ends: 5, 7.5, 10, 12.5). We don't emit a
    further window starting at 10 because the previous one already
    reached past the input length, so any extra would be pure padding.
    """
    samples = np.zeros(int(PERCH_SAMPLE_RATE * 12.0), dtype="float32")
    no_overlap = _frame_into_windows(samples, overlap_seconds=0.0)
    half_overlap = _frame_into_windows(samples, overlap_seconds=2.5)
    assert no_overlap.shape[0] == 3
    assert half_overlap.shape[0] == 4
    assert half_overlap.shape[0] > no_overlap.shape[0]


def test_frame_into_windows_clamps_overlap_above_max() -> None:
    samples = np.zeros(int(PERCH_SAMPLE_RATE * 10.0), dtype="float32")
    # Overlap >= window length would make stride 0; should be clamped to
    # WINDOW - 0.1 = 4.9 -> stride 0.1 s. Don't assert exact count, just
    # that it's finite and non-explosive.
    windows = _frame_into_windows(samples, overlap_seconds=100.0)
    assert 1 < windows.shape[0] < 1_000


def test_frame_into_windows_short_audio_pads_one_window() -> None:
    samples = np.zeros(int(PERCH_SAMPLE_RATE * 0.5), dtype="float32")
    windows = _frame_into_windows(samples, overlap_seconds=0.0)
    assert windows.shape == (1, int(PERCH_SAMPLE_RATE * PERCH_WINDOW_SECONDS))


# Note: only the tests that touch the SavedModel are @pytest.mark.slow.
# The _frame_into_windows tests below are pure numpy and run in the default
# suite, so framing-math regressions are caught on every push.


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

    48 kHz is intentional, not 32 kHz: it exercises the resample path in
    _read_audio_mono_32k that real ARU recordings will hit.
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
    settings = AnalysisSettings(min_conf=0.001, overlap=0.0, locales=("en",))
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
        preferred_lang="en",
        audio_root=audio_root,
        progress=progress,
    )

    assert len(result.campaigns) == 1
    camp = result.campaigns[0]
    assert camp.campaign_name == "c1"
    assert camp.wav_count == 1
    assert camp.detections_csv.exists()

    # Header must match what the examine panel expects.
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

    Two species are listed: one nonsense ('Pseudobird fakensis') we expect to
    never appear, and one real European tit ('Parus major'). With min_conf
    low enough to trip something, every emitted row's Scientific_Name must
    be one of those two, even though Perch's class axis is 14,795 wide.
    """
    audio_root, camp_dir = campaign_with_one_wav
    out_base = tmp_path / "out"
    settings = AnalysisSettings(min_conf=0.0001, overlap=0.0, locales=("en",))
    ci = CampaignRunInput(
        name="c1",
        folder=camp_dir,
        mode=FilterMode.LIST,
        location=None,
        # Mixed format: plain Latin on one line, BirdNET 'Sci_Common' on the other.
        species_list_text="Parus major\nPseudobird fakensis_Made Up Bird\n",
    )
    progress = _RecordingProgress()

    result = PerchRunner().run(
        campaigns=[ci],
        output_base=out_base,
        settings=settings,
        preferred_lang="en",
        audio_root=audio_root,
        progress=progress,
    )

    csv_path = result.campaigns[0].detections_csv
    import csv as _csv

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(_csv.DictReader(f))
    assert rows, "filter must not block all rows; min_conf is near zero"
    allowed = {"Parus major", "Pseudobird fakensis"}
    bad = [r["Scientific_Name"] for r in rows if r["Scientific_Name"] not in allowed]
    assert not bad, f"unexpected species leaked past LIST filter: {sorted(set(bad))[:5]}"


@pytest.mark.slow
def test_perch_runner_honors_cancellation(
    campaign_with_one_wav: tuple[Path, Path], tmp_path: Path
) -> None:
    audio_root, camp_dir = campaign_with_one_wav
    out_base = tmp_path / "out"
    settings = AnalysisSettings(min_conf=0.001, overlap=0.0, locales=("en",))
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
            preferred_lang="en",
            audio_root=audio_root,
            progress=progress,
        )
