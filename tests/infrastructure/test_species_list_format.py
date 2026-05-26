"""Unit tests for the species-list parser/formatter round-trip.

The `# must-have` markers written by the runners need to survive being
pasted back into an input species list, otherwise the documented round-
trip would break silently. These tests are duplicated across runners
because BirdNET and Perch deliberately keep their species-list helpers
side by side (same logic, separate modules).
"""

from pam_analyzer.infrastructure.birdnet_runner import (
    _format_species_lines as bn_format,
)
from pam_analyzer.infrastructure.birdnet_runner import (
    _parse_species_lines as bn_parse,
)
from pam_analyzer.infrastructure.perch_runner import (
    _format_species_lines as pc_format,
)
from pam_analyzer.infrastructure.perch_runner import (
    _parse_species_lines as pc_parse,
)


def test_parse_strips_hash_comments_birdnet() -> None:
    text = "Parus major  # must-have\nCorvus corone\n# whole-line comment\n"
    assert bn_parse(text) == frozenset({"Parus major", "Corvus corone"})


def test_parse_strips_hash_comments_perch() -> None:
    text = "Parus major  # must-have\nCorvus corone\n# whole-line comment\n"
    assert pc_parse(text) == frozenset({"Parus major", "Corvus corone"})


def test_format_tags_must_haves_only_birdnet() -> None:
    species = frozenset({"Parus major", "Corvus corone"})
    must_haves = frozenset({"Parus major"})
    out = bn_format(species, must_haves)
    assert out == "Corvus corone\nParus major  # must-have\n"


def test_format_tags_must_haves_only_perch() -> None:
    species = frozenset({"Parus major", "Corvus corone"})
    must_haves = frozenset({"Parus major"})
    out = pc_format(species, must_haves)
    assert out == "Corvus corone\nParus major  # must-have\n"


def test_format_then_parse_round_trips() -> None:
    """Writing a list with markers and feeding it back as input must yield
    the same scientific names. This is the property the marker convention
    depends on: a user can copy lines from a *-species-list.txt into a
    campaign's species_list.txt without manual cleanup."""
    species = frozenset({"Parus major", "Corvus corone", "Erithacus rubecula"})
    must_haves = frozenset({"Parus major"})
    formatted = bn_format(species, must_haves)
    assert bn_parse(formatted) == species
    assert pc_parse(formatted) == species
