"""Legacy species-name aliases expand user-authored lists onto the v3.0 axis."""

from __future__ import annotations

import pytest

from pam_analyzer.infrastructure.legacy_names import _load_map, _parse_tsv, expand_species


def test_parse_skips_comments_and_blank_lines():
    assert _parse_tsv("# header\n\nAccipiter gentilis\tAstur gentilis\n") == {
        "Accipiter gentilis": "Astur gentilis"
    }


@pytest.mark.parametrize(
    "text, match",
    [
        ("one-column\n", "2 tab-separated columns"),
        ("a\tb\tc\n", "2 tab-separated columns"),
        ("Accipiter gentilis\tAstur gentilis\nAccipiter gentilis\tOther name\n", "conflicting alias"),
    ],
)
def test_parse_rejects_malformed_rows(text, match):
    with pytest.raises(ValueError, match=match):
        _parse_tsv(text)


def test_expand_adds_the_current_spelling():
    assert expand_species(frozenset({"Accipiter gentilis"})) == {
        "Accipiter gentilis",
        "Astur gentilis",
    }


def test_expand_leaves_unknown_names_alone():
    names = frozenset({"Turdus merula", "Not a species"})
    assert expand_species(names) == names


def test_expand_of_current_axis_names_is_a_noop():
    """A list already written against v3.0 must not gain anything."""
    assert expand_species(frozenset({"Astur gentilis"})) == {"Astur gentilis"}


def test_shipped_table_is_loadable_and_one_directional():
    """The committed TSV parses, and no alias target is itself a legacy key.

    A target appearing as a key would mean a two-hop rename that expand_species
    (single lookup, no transitive closure) would only follow halfway.
    """
    aliases = _load_map()
    assert len(aliases) > 100
    assert not set(aliases.values()) & set(aliases)
