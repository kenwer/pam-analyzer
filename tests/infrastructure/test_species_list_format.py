"""Unit tests for the species-list parser/formatter round-trip.

The `# must-have` markers written by the runners need to survive being
pasted back into an input species list, otherwise the documented round-
trip would break silently.
"""

from pam_analyzer.domain.species_filter import species_list_lines
from pam_analyzer.infrastructure._analysis_helpers import _format_species_lines


def test_parse_strips_hash_comments() -> None:
    text = "Parus major  # must-have\nCorvus corone\n# whole-line comment\n"
    assert species_list_lines(text) == frozenset({"Parus major", "Corvus corone"})


def test_format_tags_must_haves_only() -> None:
    species = frozenset({"Parus major", "Corvus corone"})
    must_haves = frozenset({"Parus major"})
    out = _format_species_lines(species, must_haves)
    assert out == "Corvus corone\nParus major  # must-have\n"


def test_format_then_parse_round_trips() -> None:
    """Writing a list with markers and feeding it back as input must yield
    the same scientific names. This is the property the marker convention
    depends on: a user can copy lines from an applied-species-list.txt into a
    campaign's species_list.txt without manual cleanup."""
    species = frozenset({"Parus major", "Corvus corone", "Erithacus rubecula"})
    must_haves = frozenset({"Parus major"})
    formatted = _format_species_lines(species, must_haves)
    assert species_list_lines(formatted) == species


def test_lines_come_back_verbatim() -> None:
    """The splitter strips comments and blanks and nothing else.

    A line carrying an underscore is either one of BirdNET's
    'Scientific_Common' entries or one whole Perch sound-event label, and only
    the running engine's list format settles which. That decision belongs to
    the runner, so a line must reach it intact.
    """
    text = "Turdus merula_Eurasian Blackbird\n\n  Acoustic_guitar  # keep\n"
    assert species_list_lines(text) == frozenset(
        {"Turdus merula_Eurasian Blackbird", "Acoustic_guitar"}
    )
