"""Slow guard: the committed crosswalk still matches its generator.

Loads the live BirdNET and Perch label axes (hence slow, it downloads the geo
and Perch models on a cold cache) and reruns the build script's assembly. Fails
if any committed rename now points off an axis (a stale entry after a model
version bump) or if the committed TSV has drifted from what the script produces.

Run on demand with:

    uv run poe test -m slow

The fast, model-free checks on the same table live in test_taxonomy_crosswalk.py.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path

import pytest

from pam_analyzer.infrastructure.taxonomy_crosswalk import _load_map


def _load_build_table() -> Callable[[], tuple[list[tuple[str, str]], list[str], list]]:
    """Import build_table from scripts/, which is not an importable package."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "build_taxonomy_crosswalk.py"
    spec = importlib.util.spec_from_file_location("build_taxonomy_crosswalk", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_table


@pytest.mark.slow
def test_committed_crosswalk_matches_generator() -> None:
    build_table = _load_build_table()
    pairs, warnings, _review = build_table()

    # Every generated pair validated against both live axes: an empty warning
    # list means no committed name has fallen off its axis.
    assert warnings == [], f"crosswalk names off their axis: {warnings}"

    # The shipped TSV must equal what the generator now produces, so a version
    # bump that changes the axes cannot leave the file silently out of date.
    _load_map.cache_clear()
    committed = sorted(_load_map().birdnet_to_perch.items())
    assert committed == pairs
