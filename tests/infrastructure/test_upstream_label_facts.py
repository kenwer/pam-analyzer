"""Upstream facts this app's species namespace depends on.

The counts are pinned rather than forbidden. A model can carry one species
under two class names, either because it predates a lump in BirdNET+ Taxonomy
or, like the v3.0 developer preview, because its label file had duplicates.
The label maps and the per-segment ranker merge those. What must not happen
silently is such a number changing under a model or taxonomy update, which is
exactly the event that produced the bug docs/one-species-namespace.md
describes.

When one of these fails, regenerate the alias table with
scripts/build_species_aliases.py and update the number here in the same
change.

Marked slow like the other tests that reach the model's label files.
"""

from __future__ import annotations

import pytest

from pam_analyzer.infrastructure import birdnet_2_4_onnx
from pam_analyzer.infrastructure.birdnet_lib import (
    TAXONOMY_V2_4,
    TAXONOMY_V3_0,
    _BirdnetV3_0,
    split_sci_common,
    v3_0_labels,
)
from pam_analyzer.infrastructure.perch_onnx import label_set
from pam_analyzer.infrastructure.species_names import canonical


def _classes_shadowed_by_a_sibling(labels: set[str]) -> set[str]:
    """Labels that canonicalise onto a different label of the same model."""
    return {n for n in labels if canonical(n) != n and canonical(n) in labels}


def _v3_0_raw_scientific_names(kind: str) -> set[str]:
    """Read from the label file, because TaxonomyServices canonicalises every
    name before handing it over.
    """
    return {split_sci_common(e)[0] for e in v3_0_labels("en_us", kind)}


def _v2_4_raw_scientific_names() -> set[str]:
    """v2.4's own label file, not TAXONOMY_V2_4's output.

    TaxonomyServices already runs every name through canonical() before
    handing it back, so every key would trivially satisfy canonical(n) == n
    and the shadowed-class check could never fail against that source.
    """
    return {split_sci_common(entry)[0] for entry in birdnet_2_4_onnx.labels("acoustic", "en_us")}


@pytest.mark.slow
@pytest.mark.parametrize(
    "labels, expected",
    [
        (_v2_4_raw_scientific_names, 29),
        (lambda: _v3_0_raw_scientific_names("acoustic"), 0),
        (lambda: set(label_set()), 72),
        (lambda: _v3_0_raw_scientific_names("geo"), 19),
    ],
    ids=["v2.4", "v3.0", "perch", "v3.0-geo"],
)
def test_species_carried_under_two_class_names(labels, expected) -> None:
    assert len(_classes_shadowed_by_a_sibling(labels())) == expected


@pytest.mark.slow
def test_v3_has_16_sound_classes() -> None:
    assert len(TAXONOMY_V3_0._sound_classes()) == 16


@pytest.mark.slow
def test_v3_sound_classes_pass_the_location_filter() -> None:
    """The geo model has no entry for them, so without the union every one
    would be dropped as out-of-region.
    """
    allowed = TAXONOMY_V3_0.region_species_scientific(48.52, 9.06, 20)

    assert {"Siren", "Engine", "Gun", "Music", "Fire"} <= allowed
    assert TAXONOMY_V3_0._sound_classes() <= allowed


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
def test_v2_4_polish_map_names_the_lumped_species_by_the_reference_taxonomy() -> None:
    """BirdNET+ Taxonomy lumps Lichmera limbata, the repeated line, into
    Lichmera indistincta. The merged entry carries the reference taxonomy's name.
    """
    v2_4 = TAXONOMY_V2_4.locale_label_map("pl")

    assert "Lichmera limbata" not in v2_4
    assert v2_4["Lichmera indistincta"] == TAXONOMY_V3_0.locale_label_map("pl")["Lichmera indistincta"]


@pytest.mark.slow
def test_v3_label_lookup_does_not_need_the_model(monkeypatch) -> None:
    """A locale lookup reads label files only, so it never downloads the 3.0 weights."""
    from birdnet.acoustic.models.v3_0.onnx import AcousticOnnxDownloaderV3_0

    def _no_download(*args, **kwargs):
        raise AssertionError("a label lookup reached the model download")

    monkeypatch.setattr(AcousticOnnxDownloaderV3_0, "_check_acoustic_model_available", classmethod(lambda cls, p: False))
    monkeypatch.setattr(AcousticOnnxDownloaderV3_0, "_download_model", classmethod(_no_download))
    # A fresh instance, so no map cached by an earlier test hides the read
    assert _BirdnetV3_0().locale_label_map("de")
