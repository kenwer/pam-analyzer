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


def test_canonical_rewrites_a_superseded_spelling():
    assert canonical("Accipiter gentilis") == "Astur gentilis"


def test_canonical_is_the_identity_for_a_current_spelling():
    assert canonical("Astur gentilis") == "Astur gentilis"


def test_canonical_is_the_identity_for_an_unknown_name():
    assert canonical("Acoustic_guitar") == "Acoustic_guitar"
    assert canonical("Turdus merula") == "Turdus merula"


def test_canonical_is_stable_under_reapplication():
    once = canonical("Charadrius dubius")
    assert canonical(once) == once


def test_canonical_set_maps_every_member():
    assert canonical_set(["Accipiter gentilis", "Turdus merula"]) == frozenset(
        {"Astur gentilis", "Turdus merula"}
    )


def test_canonical_set_collapses_two_spellings_of_one_bird():
    """The v3.0 label file carries both spellings for 27 birds."""
    assert canonical_set(["Charadrius dubius", "Thinornis dubius"]) == frozenset(
        {"Thinornis dubius"}
    )


def test_shipped_table_is_loadable_and_one_directional():
    table = _load_map()
    assert len(table) == 175
    assert table["Accipiter gentilis"] == "Astur gentilis"
    assert table["Charadrius dubius"] == "Thinornis dubius"
    assert not set(table) & set(table.values())
