"""Unit tests for the species-list parser/formatter round-trip.

The `# must-have` markers written by the runners need to survive being
pasted back into an input species list, otherwise the documented round-
trip would break silently.
"""

from pam_analyzer.infrastructure._analysis_helpers import (
    _format_species_lines,
    parse_species_lines,
)


def test_parse_strips_hash_comments() -> None:
    text = "Parus major  # must-have\nCorvus corone\n# whole-line comment\n"
    assert parse_species_lines(text) == frozenset({"Parus major", "Corvus corone"})


def test_format_tags_must_haves_only() -> None:
    species = frozenset({"Parus major", "Corvus corone"})
    must_haves = frozenset({"Parus major"})
    out = _format_species_lines(species, must_haves)
    assert out == "Corvus corone\nParus major  # must-have\n"


def test_format_then_parse_round_trips() -> None:
    """Writing a list with markers and feeding it back as input must yield
    the same scientific names. This is the property the marker convention
    depends on: a user can copy lines from a *-species-list.txt into a
    campaign's species_list.txt without manual cleanup."""
    species = frozenset({"Parus major", "Corvus corone", "Erithacus rubecula"})
    must_haves = frozenset({"Parus major"})
    formatted = _format_species_lines(species, must_haves)
    assert parse_species_lines(formatted) == species
