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
    from pam_analyzer.infrastructure.birdnet_lib import v3_0_labels

    raw = {e.partition("_")[0] for e in v3_0_labels("en_us")}

    assert len(_classes_shadowed_by_a_sibling(raw)) == 27


@pytest.mark.slow
@pytest.mark.parametrize("kind", ["acoustic", "geo"])
def test_v2_4_polish_labels_keep_every_class(kind: str) -> None:
    """Upstream's v2.4 pl.txt repeats one line, at the position of Lichmera limbata.

    Labels pair with model outputs by position, so a reader that dropped the
    repeat would leave 6521 names for 6522 outputs.
    """
    labels = birdnet_2_4_onnx.labels(kind, "pl")

    assert len(labels) == 6522
    assert "Lichmera limbata_Indonesian Honeyeater" in labels


@pytest.mark.slow
def test_v2_4_polish_map_falls_back_to_english_for_the_repeated_line() -> None:
    from pam_analyzer.infrastructure.birdnet_lib import TAXONOMY_V2_4

    assert TAXONOMY_V2_4.locale_label_map("pl")["Lichmera limbata"] == "Indonesian Honeyeater"


@pytest.mark.slow
def test_v3_label_lookup_does_not_need_the_model(monkeypatch) -> None:
    """A locale lookup reads label files only, so it never downloads the 3.0 weights."""
    from birdnet.acoustic.models.v3_0.onnx import AcousticOnnxDownloaderV3_0

    from pam_analyzer.infrastructure import birdnet_lib

    def _no_download(*args, **kwargs):
        raise AssertionError("a label lookup reached the model download")

    monkeypatch.setattr(AcousticOnnxDownloaderV3_0, "_check_acoustic_model_available", classmethod(lambda cls, p: False))
    monkeypatch.setattr(AcousticOnnxDownloaderV3_0, "_download_model", classmethod(_no_download))
    birdnet_lib._locale_label_map.cache_clear()
    try:
        assert birdnet_lib._locale_label_map("3.0", "de")
    finally:
        birdnet_lib._locale_label_map.cache_clear()


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
