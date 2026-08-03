"""Unit tests for the BirdNET <-> Perch taxonomy crosswalk.

Parsing is tested against inline text so the assertions do not depend on how
many rename pairs the bundled table currently holds. A separate test loads the
real table only to confirm it is well-formed.
"""

import pytest

from pam_analyzer.infrastructure import taxonomy_crosswalk as tc
from pam_analyzer.infrastructure.taxonomy_crosswalk import (
    BIRDNET_2_4,
    PERCH_2_0,
    _load_map,
    _parse_tsv,
    _warn_unknown_axis,
    expand_species,
    to_axis,
)

_SAMPLE = (
    "# birdnet_scientific\tperch_scientific\n"
    "Accipiter gentilis\tAstur gentilis\n"
    "\n"  # blank line ignored
    "Accipiter fasciatus\tAstur fasciatus\n"
)


def test_parse_builds_directional_and_symmetric_maps() -> None:
    maps = _parse_tsv(_SAMPLE)
    assert maps.birdnet_to_perch["Accipiter gentilis"] == "Astur gentilis"
    assert maps.perch_to_birdnet["Astur gentilis"] == "Accipiter gentilis"
    assert maps.synonyms["Accipiter gentilis"] == frozenset(
        {"Accipiter gentilis", "Astur gentilis"}
    )
    assert maps.synonyms["Astur gentilis"] == frozenset(
        {"Accipiter gentilis", "Astur gentilis"}
    )


@pytest.mark.parametrize(
    "text",
    [
        "one column only\n",  # missing tab
        "a\tb\tc\n",  # too many columns
        "\tPerch name\n",  # empty birdnet column
        "Same\tSame\n",  # self-pair
        "A\tX\nA\tY\n",  # duplicate birdnet key
        "A\tX\nB\tX\n",  # duplicate perch key
    ],
)
def test_parse_rejects_malformed_rows(text: str) -> None:
    with pytest.raises(ValueError):
        _parse_tsv(text)


def test_expand_and_to_axis_use_the_parsed_table(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_map.cache_clear()
    monkeypatch.setattr(tc, "_load_map", lambda: _parse_tsv(_SAMPLE))

    # expand_species: adds the cross-axis equivalent both directions, and
    # leaves a name with no rename as just itself.
    assert expand_species(frozenset({"Accipiter gentilis"})) == frozenset(
        {"Accipiter gentilis", "Astur gentilis"}
    )
    assert expand_species(frozenset({"Astur gentilis"})) == frozenset(
        {"Accipiter gentilis", "Astur gentilis"}
    )
    assert expand_species(frozenset({"Turdus merula"})) == frozenset({"Turdus merula"})

    # to_axis: rewrites into the target taxonomy, identity for the already-in-
    # axis case and for a name absent from the table.
    assert to_axis("Astur gentilis", BIRDNET_2_4) == "Accipiter gentilis"
    assert to_axis("Accipiter gentilis", PERCH_2_0) == "Astur gentilis"
    assert to_axis("Accipiter gentilis", BIRDNET_2_4) == "Accipiter gentilis"
    assert to_axis("Turdus merula", PERCH_2_0) == "Turdus merula"


def test_to_axis_unknown_target_passes_through_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An axis the build no longer offers must not fail a run: names pass through
    # un-normalized, and the miss is logged once (see _warn_unknown_axis).
    _warn_unknown_axis.cache_clear()
    with caplog.at_level("WARNING"):
        assert to_axis("Accipiter gentilis", "BirdNET-3.0") == "Accipiter gentilis"
    assert "unknown target axis" in caplog.text


def test_bundled_table_is_well_formed() -> None:
    _load_map.cache_clear()
    maps = _load_map()  # raises if the shipped TSV has a malformed row
    # Every stored pair round-trips through both directional maps.
    for birdnet, perch in maps.birdnet_to_perch.items():
        assert maps.perch_to_birdnet[perch] == birdnet
