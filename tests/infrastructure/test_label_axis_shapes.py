"""The label-shape fact the species-list reader is built on.

A list line is read against the running model's own axis: a line that already
names one of its classes is that class, and anything else is a BirdNET
'Scientific_Common' entry that loses its common-name half. That is only sound
while no BirdNET list entry is spelled like a Perch label, because such a line
would be kept whole on a Perch run and then match nothing the model emits.

The label files are upstream data, so the property is asserted here instead of
assumed: a file that changed shape should fail loudly rather than quietly
mis-read a list.

Marked slow like the other tests that reach the model's label files.
"""

from __future__ import annotations

import pytest

from pam_analyzer.infrastructure.birdnet_2_4_taxonomy import TAXONOMY as TAXONOMY_V2_4
from pam_analyzer.infrastructure.birdnet_3_0_taxonomy import TAXONOMY as TAXONOMY_V3_0
from pam_analyzer.infrastructure.perch_onnx import label_set


@pytest.mark.slow
@pytest.mark.parametrize("taxonomy", [TAXONOMY_V2_4, TAXONOMY_V3_0], ids=["v2.4", "v3.0"])
def test_no_birdnet_list_entry_is_spelled_like_a_perch_label(taxonomy) -> None:
    """A user's list carries whole 'Scientific_Common' entries, which is the
    only shape a Perch run could mistake for one of its own class names. The
    bare scientific name a user may equally have typed needs no such check: if
    it is a Perch class, keeping it whole is the right reading.
    """
    labels = taxonomy.locale_label_map("en_us")
    assert labels, "label read failed, the map came back empty"

    entries = {f"{sci}_{common}" for sci, common in labels.items()}
    assert entries & label_set() == set()


@pytest.mark.slow
def test_v3_label_map_is_keyed_on_canonical_names() -> None:
    """v3.0 carries Charadrius dubius and Thinornis dubius as separate
    classes for one bird. The map keeps only the canonical key.
    """
    labels = TAXONOMY_V3_0.locale_label_map("en_us")

    assert "Thinornis dubius" in labels
    assert "Charadrius dubius" not in labels


@pytest.mark.slow
def test_v3_0_names_the_canonical_species() -> None:
    assert TAXONOMY_V3_0.locale_label_map("de")["Thinornis dubius"] == "Flussregenpfeifer"


@pytest.mark.slow
def test_a_v2_4_translation_survives_being_promoted() -> None:
    """v2.4 has only the superseded spelling, so its entry moves under the
    canonical key and must carry its translation with it.
    """
    assert TAXONOMY_V2_4.locale_label_map("de")["Thinornis dubius"] == "Flussregenpfeifer"
    assert "Charadrius dubius" not in TAXONOMY_V2_4.locale_label_map("de")


@pytest.mark.slow
@pytest.mark.parametrize("lang", ["en_us", "de"])
def test_v2_4_takes_its_common_names_from_the_reference_taxonomy(lang: str) -> None:
    """One species gets one name, whichever engine found it."""
    v2_4 = TAXONOMY_V2_4.locale_label_map(lang)
    v3_0 = TAXONOMY_V3_0.locale_label_map(lang)

    differing = {k: (v2_4[k], v3_0[k]) for k in v2_4.keys() & v3_0.keys() if v3_0[k] and v2_4[k] != v3_0[k]}

    assert differing == {}
    assert v2_4["Acanthis flammea"] == v3_0["Acanthis flammea"]


@pytest.mark.slow
def test_the_reference_names_do_not_widen_the_v2_4_axis() -> None:
    """The known-species set derives from the map's keys, and v3.0 has thousands v2.4 lacks."""
    assert "Abeillia abeillei" not in TAXONOMY_V2_4.locale_label_map("en_us")
    assert "Abeillia abeillei" not in TAXONOMY_V2_4.known_species_scientific()


@pytest.mark.slow
def test_known_species_is_canonical_because_it_derives_from_the_label_map() -> None:
    assert "Charadrius dubius" not in TAXONOMY_V3_0.known_species_scientific()
    assert "Thinornis dubius" in TAXONOMY_V3_0.known_species_scientific()
