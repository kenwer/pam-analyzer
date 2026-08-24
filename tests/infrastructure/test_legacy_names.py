"""Legacy species-name aliases reconcile both engines onto one output axis."""

from __future__ import annotations

import pytest

from pam_analyzer.infrastructure.legacy_names import (
    BIRDNET_2_4,
    BIRDNET_3_0,
    TAXONOMIES,
    _load_map,
    _parse_tsv,
    expand_species,
    to_axis,
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


def test_expand_adds_the_legacy_spelling():
    """A list written against v3.0 has to match a v2.4 run too.

    The v2.4 engine matches detections on its own axis, so a must-have entry
    typed under the current spelling only survives that run if the legacy
    spelling is expanded alongside it.
    """
    assert expand_species(frozenset({"Astur gentilis"})) == {
        "Astur gentilis",
        "Accipiter gentilis",
    }


def test_expand_is_stable_under_reapplication():
    """Expanding an expanded set adds nothing further.

    Both directions are applied in one pass, so the result is already closed.
    A second pass growing the set would mean a name is being followed
    transitively, which the single-lookup design does not support.
    """
    once = expand_species(frozenset({"Accipiter gentilis"}))
    assert expand_species(once) == once


def test_shipped_table_is_loadable_and_one_directional():
    """The committed TSV parses, and no alias target is itself a legacy key.

    _parse_tsv now rejects such a chain outright, so loading the shipped
    table at all is most of the assertion. The explicit check stays because
    it states the property the rest of this module relies on.
    """
    aliases = _load_map()
    assert len(aliases) > 100
    assert not set(aliases.values()) & set(aliases)


def test_parse_rejects_a_chained_rename():
    """A name that is legacy in one row and current in another is ambiguous.

    to_axis applies one map in one pass, so a chain A -> B -> C would resolve
    to B or C depending on which map ran, and B belongs to neither axis.
    """
    text = "A one\tB two\nB two\tC three\n"
    with pytest.raises(ValueError, match="both a legacy and a current spelling"):
        _parse_tsv(text)


def test_to_axis_rewrites_in_both_directions():
    assert to_axis("Accipiter gentilis", BIRDNET_3_0) == "Astur gentilis"
    assert to_axis("Astur gentilis", BIRDNET_2_4) == "Accipiter gentilis"


def test_to_axis_is_a_no_op_for_a_name_already_on_the_target_axis():
    """Each runner applies to_axis unconditionally, including to its own axis.

    v2.4 output hitting the v2.4 axis, and v3.0 output hitting the v3.0 axis,
    both have to pass through untouched, or a runner would need to know which
    axis it emits before calling.
    """
    assert to_axis("Accipiter gentilis", BIRDNET_2_4) == "Accipiter gentilis"
    assert to_axis("Astur gentilis", BIRDNET_3_0) == "Astur gentilis"


@pytest.mark.parametrize("target", TAXONOMIES)
def test_to_axis_leaves_unrenamed_names_alone(target):
    assert to_axis("Turdus merula", target) == "Turdus merula"


def test_to_axis_round_trips():
    for legacy, current in _load_map().items():
        assert to_axis(to_axis(legacy, BIRDNET_3_0), BIRDNET_2_4) == legacy
        assert to_axis(to_axis(current, BIRDNET_2_4), BIRDNET_3_0) == current


def test_to_axis_passes_names_through_for_an_unknown_axis(caplog):
    """A project.toml naming a dropped axis (Perch-2.0) must not fail the run.

    Output falls back to the model's own names, which is wrong but readable,
    and the warning says so once rather than once per detection row.
    """
    with caplog.at_level("WARNING"):
        assert to_axis("Accipiter gentilis", "Perch-2.0") == "Accipiter gentilis"
    assert "unknown target axis" in caplog.text


def test_default_axis_is_first_so_a_combo_falls_back_to_it():
    """ProjectPanel selects index 0 when it cannot find the stored axis."""
    assert TAXONOMIES[0] == BIRDNET_3_0
