"""Tests for PerchRunner, focused on what differs from the BirdNET runners.

The shared pipeline (progress phases, CSV schema, cancellation, species
filtering) is covered once in test_birdnet_runner.py and lives in
BaseAnalysisRunner, so it is not repeated here.

Two things are specific to this runner. Perch emits raw class logits rather
than probabilities, and its logits sit far from zero, so a calibration offset
turns them into a Confidence comparable with what the BirdNET runners write.
And Perch labels its classes with bare scientific names, where BirdNET uses
'Scientific_Common', so the row parsing that works for one corrupts the other.

The _parse_row cases run fast because that hook takes its label maps as
arguments and never touches the model.
"""

from __future__ import annotations

import csv as _csv
import math
from pathlib import Path

import pytest

from pam_analyzer.domain import (
    AnalysisSettings,
    Campaign,
    FilterMode,
    RunStatus,
)
from pam_analyzer.infrastructure.birdnet_lib import TAXONOMY_V3_0
from pam_analyzer.infrastructure.perch_runner import (
    MODEL_KEY,
    SESSION_THREADS,
    PerchRunner,
    _n_workers,
    _perch_logit_threshold,
    _perch_logit_to_prob,
)
from tests.infrastructure.test_birdnet_runner import (
    _campaign_with_one_wav,
    _RecordingProgress,
)

# The offset the calibration settled on. Stated here rather than imported so a
# change to the constant has to be a deliberate edit of this expectation too.
CALIBRATED_OFFSET = 11.2

# Perch shares BirdNET v3.0's spelling for this bird, which is also the
# canonical one.
CURRENT_NAME = "Astur gentilis"

# A real Perch class from the FSD50k half of its label set. It is not a species
# and it contains underscores, which is exactly what a BirdNET-style
# 'Scientific_Common' split would destroy.
SOUND_EVENT_LABEL = "Accelerating_and_revving_and_vroom"


def _parse_one(
    species_name: str,
    confidence: float,
    preferred_lang_map: dict[str, str] | None = None,
):
    """Run _parse_row on a single synthetic result row.

    Needs no model: the hook takes its label maps as arguments. `confidence`
    is a raw logit here, because that is what the lib hands over when the
    session is opened with apply_sigmoid=False.
    """
    return PerchRunner()._parse_row(
        {
            "species_name": species_name,
            "input": "/tmp/x.WAV",
            "start_time": 0.0,
            "end_time": 5.0,
            "confidence": confidence,
        },
        preferred_lang_map=preferred_lang_map or {},
        locale_maps={"en_us": {}},
        settings=AnalysisSettings(locales=("en_us",)),
    )


def test_threshold_and_probability_are_inverses() -> None:
    """A probability mapped into logit space and back is unchanged.

    The runner thresholds in logit space and reports in probability space, so
    these two have to be exact inverses. If they drift apart, a run silently
    keeps a different set of rows than the min_conf the user asked for.
    """
    for min_conf in (0.05, 0.2, 0.5, 0.9):
        assert math.isclose(_perch_logit_to_prob(_perch_logit_threshold(min_conf)), min_conf)


def test_the_offset_puts_one_half_at_the_calibrated_logit() -> None:
    """Confidence 0.5 sits at the calibrated offset, not at logit zero.

    Perch's head emits positive logits everywhere: silence alone sits near
    +4.5. Without the offset every window would report its top classes at
    ~0.99 and the Confidence column would be meaningless.
    """
    assert math.isclose(_perch_logit_threshold(0.5), CALIBRATED_OFFSET)
    assert math.isclose(_perch_logit_to_prob(CALIBRATED_OFFSET), 0.5)


def test_parse_row_reports_a_probability_not_a_logit() -> None:
    """The CSV's Confidence column stays in 0-1, matching the BirdNET runners.

    The lib hands over a raw logit because the session sets apply_sigmoid=False,
    so a runner that passed it straight through would write values like 11.2
    into a column every other engine fills with probabilities.
    """
    parsed = _parse_one(CURRENT_NAME, confidence=CALIBRATED_OFFSET)
    assert math.isclose(parsed.confidence, 0.5)


def test_parse_row_keeps_underscores_in_non_species_labels() -> None:
    """A label is a whole name, never a 'Scientific_Common' pair to split.

    Perch's label set includes FSD50k sound events whose names contain
    underscores. Splitting on the first one, as the BirdNET runners must, would
    turn this class into 'Accelerating' and invent a common name from the rest.
    """
    parsed = _parse_one(SOUND_EVENT_LABEL, confidence=CALIBRATED_OFFSET)
    assert parsed.scientific_name == SOUND_EVENT_LABEL


def test_parse_row_takes_common_names_from_the_v3_labels() -> None:
    """Perch ships no common names, so they come from BirdNET v3.0's label map.

    The lookup keys on the canonical spelling, which for this bird is the
    v3.0 one Perch itself emits.
    """
    parsed = _parse_one(
        CURRENT_NAME,
        confidence=CALIBRATED_OFFSET,
        preferred_lang_map={CURRENT_NAME: "Northern Goshawk"},
    )
    assert parsed.preferred_common == "Northern Goshawk"


def test_runner_uses_the_v3_taxonomy() -> None:
    """Perch's axis converges with BirdNET v3.0's, so it reuses those services.

    Measured on the shipped label sets: 10916 of v3.0's 11560 classes are
    spelled identically in Perch. That is what lets the v3.0 geo model supply a
    usable allow-list without a Perch-specific crosswalk.
    """
    assert PerchRunner.taxonomy is TAXONOMY_V3_0


def test_model_key_names_the_perch_release() -> None:
    """The key becomes part of the CSV filename on the user's disk."""
    assert MODEL_KEY == "Perch-2.0"
    assert PerchRunner.model_key == MODEL_KEY


def test_worker_split_fills_the_machine_without_oversubscribing() -> None:
    """Workers times threads stays within the physical cores.

    Perch resolves to roughly 1.25 GB resident per worker, so this split is a
    memory decision as much as a speed one: one worker per core measured 98.0
    seg/s at 18.9 GB against 96.5 seg/s at 9.4 GB for this split.
    """
    import psutil

    cores = psutil.cpu_count(logical=False) or 1
    assert SESSION_THREADS == 2
    assert 1 <= _n_workers() * SESSION_THREADS <= max(cores, SESSION_THREADS)


@pytest.mark.slow
def test_run_writes_a_detections_csv(tmp_path: Path) -> None:
    """The runner completes a campaign and writes rows in the shared schema.

    Marked slow because it loads the 413 MB Perch export and spawns the
    inference pipeline. It is the only test here that proves the backend
    pairing, the calibration and the shared writer actually compose.
    """
    camp_dir = _campaign_with_one_wav(tmp_path, seconds=60.0)
    campaign = Campaign(name="c1", folder=camp_dir, species_filter_mode=FilterMode.LIST)
    progress = _RecordingProgress()

    result = PerchRunner().run(
        campaigns=[campaign],
        settings=AnalysisSettings(min_conf=0.2, overlap=0.0, locales=("en_us",)),
        preferred_lang="en_us",
        progress=progress,
    )

    assert result.status is RunStatus.COMPLETED
    csv_path = result.campaigns[0].detections_csv
    assert csv_path.is_file()

    with csv_path.open(encoding="utf-8") as handle:
        rows = list(_csv.DictReader(handle))

    assert all(row["Model"] == MODEL_KEY for row in rows)
    # Every Confidence has been through the calibration, so none can still be
    # a raw logit sitting above 1.
    assert all(0.0 <= float(row["Confidence"]) <= 1.0 for row in rows)
    # 5 s windows, unlike BirdNET's 3 s ones.
    assert all(
        math.isclose(float(row["End"]) - float(row["Start"]), 5.0, abs_tol=0.05) for row in rows
    )
