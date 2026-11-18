"""One species namespace: every model label resolves to one canonical name."""

from __future__ import annotations

import pytest

from pam_analyzer.infrastructure.species_names import (
    _load_map,
    _parse_tsv,
    canonical,
    canonical_set,
)


def test_parse_skips_comments_and_blank_lines():
    assert _parse_tsv("# header\n\nAccipiter gentilis\tAstur gentilis\n") == {
        "Accipiter gentilis": "Astur gentilis"
    }


@pytest.mark.parametrize(
    "text, match",
    [
        ("one-column\n", "2 tab-separated columns"),
        ("a\tb\tc\n", "2 tab-separated columns"),
        ("Accipiter gentilis\tAstur gentilis\nAccipiter gentilis\tOther\n", "conflicting alias"),
        ("A one\tB two\nB two\tC three\n", "both a superseded and a canonical spelling"),
    ],
)
def test_parse_rejects_malformed_rows(text, match):
    with pytest.raises(ValueError, match=match):
        _parse_tsv(text)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Accipiter gentilis", "Astur gentilis"),
        ("Astur gentilis", "Astur gentilis"),
        ("Acoustic_guitar", "Acoustic_guitar"),
    ],
    ids=["superseded", "current", "unknown"],
)
def test_canonical_rewrites_only_superseded_spellings(name, expected):
    assert canonical(name) == expected


def test_canonical_set_collapses_two_spellings_of_one_bird():
    """The v3.0 preview carried both spellings for 27 birds."""
    assert canonical_set(["Charadrius dubius", "Thinornis dubius"]) == frozenset(
        {"Thinornis dubius"}
    )


def test_canonical_set_collapses_a_lump():
    """BirdNET+ Taxonomy lumps the redpolls that v2.4 still keeps apart."""
    assert canonical_set(["Acanthis flammea", "Acanthis hornemanni", "Acanthis cabaret"]) == frozenset(
        {"Acanthis flammea"}
    )


@pytest.mark.parametrize(
    "superseded, current",
    [
        ("Accipiter gentilis", "Astur gentilis"),
        ("Charadrius dubius", "Thinornis dubius"),
        ("Emberiza schoeniclus", "Schoeniclus schoeniclus"),
        ("Troglodytes troglodytes", "Nannus troglodytes"),
        ("Anthropoides virgo", "Grus virgo"),
    ],
    ids=["taxonomy", "preview-duplicate", "genus-split", "v2.4-on-v3.0", "supplement"],
)
def test_shipped_table_maps_onto_the_taxonomy(superseded, current):
    assert _load_map()[superseded] == current


def test_shipped_table_is_loadable_and_one_directional():
    table = _load_map()
    assert not set(table) & set(table.values())


def test_shipped_table_keeps_a_vetoed_taxonomy_alias():
    """The taxonomy lists Perch's gayal as an alias of the Atlantic Spotted Dolphin."""
    assert "Bos frontalis" not in _load_map()


def test_shipped_table_leaves_splits_alone():
    """The eBird code of a split follows one daughter species, so mapping on it would be wrong."""
    assert "Trachyphonus purpuratus" not in _load_map()
