"""Upstream facts this app's species namespace depends on.

The counts are pinned rather than forbidden. BirdNET v3.0 carrying two classes
for one bird is expected and handled, and the v3.0 build shipped today is a
developer preview whose own README lists "Species list needs cleanup" as a
known limitation. What must not happen silently is the number changing under a
model or taxonomy update, which is exactly the event that produced the bug
docs/one-species-namespace.md describes.

When one of these fails, re-derive the alias table against the new label set
and update the number here in the same change.

Marked slow like the other tests that reach the model's label files.
"""

from __future__ import annotations

import pytest

from pam_analyzer.infrastructure import birdnet_2_4_onnx
from pam_analyzer.infrastructure.birdnet_lib import _geo_model_cached, _split_sci_common
from pam_analyzer.infrastructure.perch_onnx import label_set
from pam_analyzer.infrastructure.species_names import _load_map, canonical


def _classes_shadowed_by_a_sibling(labels: set[str]) -> set[str]:
    """Labels that canonicalise onto a different label of the same model."""
    return {n for n in labels if canonical(n) != n and canonical(n) in labels}


def _v2_4_raw_scientific_names() -> set[str]:
    """v2.4's own label file, not TAXONOMY_V2_4's output.

    TaxonomyServices already runs every name through canonical() before
    handing it back, so every key would trivially satisfy canonical(n) == n
    and the shadowed-class check could never fail against that source.
    """
    return {_split_sci_common(entry)[0] for entry in birdnet_2_4_onnx.labels("acoustic", "en_us")}


@pytest.mark.slow
@pytest.mark.parametrize(
    "labels, expected",
    [
        (_v2_4_raw_scientific_names, 0),
        (lambda: set(label_set()), 0),
    ],
    ids=["v2.4", "perch"],
)
def test_only_v3_carries_a_species_under_two_class_names(labels, expected) -> None:
    assert len(_classes_shadowed_by_a_sibling(labels())) == expected


@pytest.mark.slow
def test_v3_still_carries_exactly_27_shadowed_classes() -> None:
    """Read from the raw label file, because the label map has already merged
    them by the time TaxonomyServices hands it over.
    """
    from birdnet.acoustic.models.v3_0.onnx import AcousticOnnxDownloaderV3_0

    _, entries = AcousticOnnxDownloaderV3_0.get_model_path_and_labels("en_us", "fp32")
    raw = {e.partition("_")[0] for e in entries}

    assert len(_classes_shadowed_by_a_sibling(raw)) == 27


@pytest.mark.slow
def test_the_geo_model_carries_no_superseded_spelling() -> None:
    """The merge direction depends on this. If upstream ever added a
    superseded spelling to the geo model, canonicalising the allow-list would
    start hiding a class it should match.

    Read from the geo model directly. TaxonomyServices.region_species_scientific
    canonicalises on the way out, so asking it would pass vacuously.
    """
    geo = _geo_model_cached("3.0")
    raw = {
        _split_sci_common(n)[0]
        for n in geo.predict(0.0, 0.0, week=None, min_confidence=0.0).to_set()
    }

    assert sorted(k for k in _load_map() if k in raw) == []
