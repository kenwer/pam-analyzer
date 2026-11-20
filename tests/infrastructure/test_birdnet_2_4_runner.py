"""Tests for Birdnet24Runner, focused on what differs from the v3.0 runner.

The shared pipeline (progress phases, CSV schema, cancellation, species
filtering) is covered once in test_birdnet_3_0_runner.py and lives in
BaseAnalysisRunner, so it is not repeated here. What is specific to this
runner is that v2.4 emits names on the older eBird axis, and every name it
writes has to land on the app's canonical axis.

The end-to-end case is marked slow because it loads the v2.4 ONNX weights
that birdnet_2_4_keras_onnx builds.
"""

from __future__ import annotations

import csv as _csv
from pathlib import Path

import pytest

from pam_analyzer.domain import AnalysisSettings, Campaign, FilterMode
from pam_analyzer.infrastructure.birdnet_2_4_runner import (
    MODEL_KEY,
    Birdnet24Runner,
)
from tests.infrastructure.test_birdnet_3_0_runner import (
    _campaign_with_one_wav,
    _RecordingProgress,
)


@pytest.mark.slow
def test_run_writes_its_own_csv_on_the_current_axis(tmp_path: Path) -> None:
    """End-to-end: v2.4 writes its own CSV and no legacy spelling reaches it.

    Also pins the filename, because a campaign analysed by an older release
    already has detections-BirdNET-2.4.csv on disk and the app has to keep
    treating that file as this engine's output rather than orphaning it.
    """
    from pam_analyzer.infrastructure import birdnet_3_0_taxonomy

    folder = _campaign_with_one_wav(tmp_path, seconds=60.0)
    campaign = Campaign(name="c1", folder=folder, species_filter_mode=FilterMode.LIST)
    result = Birdnet24Runner().run(
        campaigns=[campaign],
        settings=AnalysisSettings(min_conf=0.001, overlap=0.0, locales=("en_us",)),
        preferred_lang="en_us",
        progress=_RecordingProgress(),
    )

    csv_path = result.campaigns[0].detections_csv
    assert csv_path.name == f"detections-{MODEL_KEY}.csv"

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(_csv.DictReader(f))
    names = {r["Scientific_Name"] for r in rows}
    assert names, "expected at least one row at min_conf=0.001"
    assert names <= birdnet_3_0_taxonomy.TAXONOMY.known_species_scientific()
    assert {r["Model"] for r in rows} == {MODEL_KEY}
