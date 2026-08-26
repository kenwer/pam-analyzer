"""How a runner reads one line of a user's species list into a name.

The two engines spell a list entry differently, and an underscore is the only
place they collide. BirdNET lists spell an entry 'Scientific_Common', while
Perch spells one sound-event class 'Acoustic_guitar'. Nothing in a line's
shape separates the two, because BirdNET's axes carry genus-only and
family-only classes whose scientific half is a single word.

The running model's own axis settles it, which is one rule for both engines
rather than one each: a line that already names a class the model emits is
that class, and anything else loses its common-name half. See
test_label_axis_shapes for the upstream property that makes it sound.

The BirdNET cases are slow because reading a line now consults the axis, which
reaches the model's label files. Perch's label list ships with this package, so
its cases stay fast and cover both branches of the rule on their own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pam_analyzer.domain import FilterMode, SpeciesFilter
from pam_analyzer.infrastructure.birdnet_2_4_runner import Birdnet24Runner
from pam_analyzer.infrastructure.birdnet_runner import BirdnetRunner
from pam_analyzer.infrastructure.perch_runner import PerchRunner


@pytest.mark.slow
@pytest.mark.parametrize("runner", [BirdnetRunner(), Birdnet24Runner()], ids=["v3.0", "v2.4"])
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Turdus merula_Eurasian Blackbird", "Turdus merula"),
        ("Acrididae_Short-horned Grasshoppers", "Acrididae"),
        ("Hyla_rainettes", "Hyla"),
        ("Dog_Dog", "Dog"),
        ("Parus major", "Parus major"),
    ],
)
def test_birdnet_always_drops_the_common_name_half(runner, line: str, expected: str) -> None:  # noqa: ANN001
    """Every shape a BirdNET label file produces, including the genus-only and
    family-only entries whose scientific half is one word and so looks exactly
    like a Perch sound event. No BirdNET class is spelled with an underscore,
    so none of these lines is ever on the axis as it stands."""
    assert runner._read_list_entry(line) == expected


def test_perch_keeps_a_sound_event_label_whole() -> None:
    """Splitting these would truncate 'Acoustic_guitar' to 'Acoustic', which
    the model can never emit."""
    runner = PerchRunner()
    assert runner._read_list_entry("Acoustic_guitar") == "Acoustic_guitar"
    assert runner._read_list_entry("Car_passing_by") == "Car_passing_by"


def test_perch_prefers_the_whole_label_over_a_prefix_that_is_also_a_label() -> None:
    """10 of Perch's underscored labels have a prefix that is itself a label.
    Reading 'Water_tap_and_faucet' as 'Water' would quietly swap in a class
    the user did not ask for."""
    assert PerchRunner()._read_list_entry("Water_tap_and_faucet") == "Water_tap_and_faucet"


def test_perch_still_reads_a_birdnet_list_entry() -> None:
    """A hand-written list outlives the engine it was written for, so a
    BirdNET-format list dropped on a Perch campaign has to keep working. No
    Perch class is spelled this way, so the line came from a BirdNET list."""
    assert PerchRunner()._read_list_entry("Turdus merula_Eurasian Blackbird") == "Turdus merula"


def test_perch_leaves_a_plain_name_alone() -> None:
    assert PerchRunner()._read_list_entry("Turdus merula") == "Turdus merula"


@pytest.mark.parametrize(
    ("runner", "line", "expected"),
    [
        # Split, then canonicalised: the v2.4 spelling a user typed still
        # matches the v3.0 name the running model emits, because both sides
        # of the comparison are canonical.
        pytest.param(
            BirdnetRunner(),
            "Accipiter gentilis_Northern Goshawk",
            {"Astur gentilis"},
            marks=pytest.mark.slow,
        ),
        (PerchRunner(), "Acoustic_guitar", {"Acoustic_guitar"}),
        (PerchRunner(), "Accipiter gentilis", {"Astur gentilis"}),
    ],
    ids=["birdnet-entry", "perch-sound-event", "perch-legacy-name"],
)
def test_resolve_reads_lines_then_canonicalises(runner, line: str, expected: set[str]) -> None:  # noqa: ANN001
    """The runner's contribution to the domain's ResolveNames port, exercised
    through SpeciesFilter.resolve rather than called directly, so the seam the
    runner actually hands over is the one under test."""
    sf = SpeciesFilter(mode=FilterMode.LIST, list_text=f"{line}\n")

    resolved = sf.resolve([Path("a.wav")], _no_region, runner._resolve_list_names)

    assert resolved.fixed_allowed == frozenset(expected)


def _no_region(lat: float, lon: float, week: int) -> frozenset[str]:
    return frozenset()
