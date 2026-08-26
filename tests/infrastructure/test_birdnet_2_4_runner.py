"""Tests for Birdnet24Runner, focused on what differs from the v3.0 runner.

The shared pipeline (progress phases, CSV schema, cancellation, species
filtering) is covered once in test_birdnet_runner.py and lives in
BaseAnalysisRunner, so it is not repeated here. What is specific to this
runner is the two-axis handling: v2.4 emits names on the older eBird axis,
the species filter matches on that axis, and the name written to CSV is
rewritten to whichever axis the project chose.

The _parse_row cases run fast because that hook takes its label maps as
arguments and never touches the model. The end-to-end case is marked slow
because it loads the converted v2.4 ONNX weights, which the build produces
via scripts/convert_birdnet_2_4_onnx.py.
"""

from __future__ import annotations

import csv as _csv
from pathlib import Path

import pytest

from pam_analyzer.domain import AnalysisSettings, Campaign, FilterMode
from pam_analyzer.infrastructure.birdnet_2_4_runner import (
    MODEL_KEY,
    SESSION_THREADS,
    Birdnet24Runner,
    _n_workers,
)
from tests.infrastructure.test_birdnet_runner import (
    _campaign_with_one_wav,
    _RecordingProgress,
)

# The rename this whole two-axis path exists for: v2.4 emits the first
# spelling, v3.0 the second.
LEGACY_NAME = "Accipiter gentilis"
CURRENT_NAME = "Astur gentilis"


def _parse_one(species_name: str, preferred_lang_map: dict[str, str] | None = None):
    """Run _parse_row on a single synthetic result row.

    Needs no model: the hook takes its label maps as arguments.
    """
    return Birdnet24Runner()._parse_row(
        {
            "species_name": species_name,
            "input": "/tmp/x.WAV",
            "start_time": 0.0,
            "end_time": 3.0,
            "confidence": 0.9,
        },
        preferred_lang_map=preferred_lang_map or {},
        locale_maps={"en_us": {}},
        settings=AnalysisSettings(min_conf=0.25, overlap=0.0, locales=("en_us",)),
    )


def test_legacy_name_is_canonicalised_on_output() -> None:
    """A v2.4 spelling is canonicalised, so the two engines' rows line up in
    the Examine grid under one spelling.
    """
    parsed = _parse_one(f"{LEGACY_NAME}_Eurasian Goshawk")
    assert parsed.scientific_name == CURRENT_NAME


def test_name_without_an_alias_passes_through() -> None:
    """The large majority of species are spelled the same on both axes."""
    parsed = _parse_one("Turdus merula_Eurasian Blackbird")
    assert parsed.scientific_name == "Turdus merula"


def test_common_name_lookup_keys_on_the_canonical_spelling() -> None:
    """Label maps are canonicalised too, so the lookup has to key on the
    canonical name rather than the raw label v2.4 emits.

    Keying on the raw label would miss for every renamed species and
    silently fall back to English.
    """
    parsed = _parse_one(
        f"{LEGACY_NAME}_Eurasian Goshawk", preferred_lang_map={CURRENT_NAME: "Habicht"}
    )
    assert parsed.preferred_common == "Habicht"
    assert parsed.scientific_name == CURRENT_NAME


def test_worker_split_fills_the_machine_exactly_once() -> None:
    """Workers times threads stays within the physical cores, on any machine.

    The two settings are one decision: this engine runs fewer, wider workers
    than v3.0 does, and the win comes from regrouping the same threads rather
    than from adding any. A split whose product exceeded the core count would
    reintroduce the oversubscription birdnet_onnx_threads exists to remove,
    and one that fell short would leave cores idle.
    """
    import psutil

    cores = psutil.cpu_count(logical=False) or 1
    workers = _n_workers()

    assert workers >= 1
    assert workers * SESSION_THREADS <= max(cores, SESSION_THREADS)
    assert (workers + 1) * SESSION_THREADS > cores


@pytest.mark.slow
def test_run_writes_its_own_csv_on_the_current_axis(tmp_path: Path) -> None:
    """End-to-end: v2.4 writes its own CSV and no legacy spelling reaches it.

    Also pins the filename, because a campaign analysed by an older release
    already has detections-BirdNET-2.4.csv on disk and the app has to keep
    treating that file as this engine's output rather than orphaning it.
    """
    from pam_analyzer.infrastructure.birdnet_lib import TAXONOMY_V3_0

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
    assert names <= TAXONOMY_V3_0.known_species_scientific()
    assert {r["Model"] for r in rows} == {MODEL_KEY}
