"""Unit tests for the SpeciesFilter domain module.

Resolution used to run only inside a full analysis pass (build_allowed_lookup
in infrastructure). It now lives on SpeciesFilter and is exercised here with a
fake region_species lookup, no runner and no model.
"""

from pathlib import Path

import pytest

from pam_analyzer.domain import FilterMode, LatLon, SpeciesFilter
from pam_analyzer.domain.audio_import import WEEK_YEAR_ROUND


def _fake_region(lat: float, lon: float, week: int) -> frozenset[str]:
    """Region species keyed by week so per-week resolution is observable."""
    return frozenset({f"Region sp{week}"})


def _fake_resolve(lines: frozenset[str]) -> frozenset[str]:
    """Stand-in for the infrastructure reader, with one rename and one tripwire.

    Adds the second taxonomy's spelling for a name the two generations spell
    differently, and would rewrite a region name if one were ever handed to it,
    so a test can assert the region list does not go through this port.
    """
    out = set(lines)
    if "Accipiter gentilis" in lines:
        out.add("Astur gentilis")
    if "Region sp10" in lines:
        out.add("Region EXPANDED")  # must never appear: region is not resolved
    return frozenset(out)


def test_list_mode_resolves_to_one_fixed_set_for_every_file() -> None:
    sf = SpeciesFilter(
        mode=FilterMode.LIST,
        list_text="Parus major\nTurdus merula\n# a comment\n",
    )
    resolved = sf.resolve([Path("week_10/a.wav"), Path("b.wav")], _fake_region, _fake_resolve)

    expected = frozenset({"Parus major", "Turdus merula"})
    assert resolved.fixed_allowed == expected
    # Same set regardless of the file's week.
    assert resolved.allowed_for(Path("week_10/a.wav")) == expected
    assert resolved.allowed_for(Path("b.wav")) == expected


def test_list_mode_with_no_text_keeps_every_row() -> None:
    resolved = SpeciesFilter(mode=FilterMode.LIST).resolve(
        [Path("a.wav")], _fake_region, _fake_resolve
    )
    assert resolved.allowed_for(Path("a.wav")) is None


def test_location_mode_resolves_per_week_and_unions_must_haves() -> None:
    sf = SpeciesFilter(
        mode=FilterMode.LOCATION,
        location=LatLon(48.0, 11.0),
        must_have_text="Aquila chrysaetos\n",
    )
    files = [Path("ARU/week_10/a.wav"), Path("ARU/week_22/b.wav")]
    resolved = sf.resolve(files, _fake_region, _fake_resolve)

    # Each week gets its own regional set, with the must-have unioned on top.
    assert resolved.allowed_for(Path("ARU/week_10/a.wav")) == frozenset(
        {"Region sp10", "Aquila chrysaetos"}
    )
    assert resolved.allowed_for(Path("ARU/week_22/b.wav")) == frozenset(
        {"Region sp22", "Aquila chrysaetos"}
    )
    assert resolved.must_haves == frozenset({"Aquila chrysaetos"})


def test_location_mode_files_without_week_folder_use_year_round() -> None:
    sf = SpeciesFilter(mode=FilterMode.LOCATION, location=LatLon(0.0, 0.0))
    resolved = sf.resolve([Path("ARU/loose.wav")], _fake_region, _fake_resolve)

    # week=-1 is the birdnet 'year-round' key, so a file outside any week_NN
    # folder resolves against it.
    assert resolved.allowed_for(Path("ARU/loose.wav")) == frozenset(
        {f"Region sp{WEEK_YEAR_ROUND}"}
    )


def test_location_mode_without_location_keeps_every_row() -> None:
    resolved = SpeciesFilter(mode=FilterMode.LOCATION, location=None).resolve(
        [Path("a.wav")], _fake_region, _fake_resolve
    )
    assert resolved.allowed_for(Path("a.wav")) is None


def test_list_mode_reads_the_user_list_through_the_port() -> None:
    sf = SpeciesFilter(mode=FilterMode.LIST, list_text="Accipiter gentilis\nParus major\n")
    resolved = sf.resolve([Path("a.wav")], _fake_region, _fake_resolve)

    # The BirdNET-spelled entry now also matches the Perch spelling.
    assert resolved.fixed_allowed == frozenset(
        {"Accipiter gentilis", "Astur gentilis", "Parus major"}
    )


def test_location_mode_reads_must_haves_through_the_port_but_not_region() -> None:
    sf = SpeciesFilter(
        mode=FilterMode.LOCATION,
        location=LatLon(48.0, 11.0),
        must_have_text="Accipiter gentilis\n",
    )
    resolved = sf.resolve(
        [Path("ARU/week_10/a.wav")], _fake_region, _fake_resolve
    )

    allowed = resolved.allowed_for(Path("ARU/week_10/a.wav"))
    # The must-have text went through the port.
    assert {"Accipiter gentilis", "Astur gentilis"} <= allowed
    # The regional list is left on BirdNET's axis: the port's region
    # tripwire never fires because region output is not passed through it.
    assert "Region EXPANDED" not in allowed
    assert "Region sp10" in allowed
    assert resolved.must_haves == frozenset({"Accipiter gentilis", "Astur gentilis"})


def test_list_mode_save_load_round_trip(tmp_path: Path) -> None:
    SpeciesFilter(mode=FilterMode.LIST, list_text="Parus major\n").save(tmp_path)
    loaded = SpeciesFilter.load(tmp_path, FilterMode.LIST, None)

    assert loaded.list_text == "Parus major\n"
    # LIST mode never touches the must-have sidecar.
    assert loaded.must_have_text == ""


def test_location_mode_save_load_round_trip(tmp_path: Path) -> None:
    loc = LatLon(48.0, 11.0)
    SpeciesFilter(
        mode=FilterMode.LOCATION, location=loc, must_have_text="Aquila chrysaetos\n"
    ).save(tmp_path)
    loaded = SpeciesFilter.load(tmp_path, FilterMode.LOCATION, loc)

    assert loaded.must_have_text == "Aquila chrysaetos\n"
    assert loaded.location == loc
    assert loaded.list_text == ""


def test_resolve_requires_a_name_reader() -> None:
    """No production caller relies on a default, and an identity default would
    silently match nothing for a BirdNET-format list. Better to not have one."""
    sf = SpeciesFilter(mode=FilterMode.LIST, list_text="Parus major\n")
    with pytest.raises(TypeError):
        sf.resolve([Path("a.wav")], _fake_region)  # type: ignore[call-arg]


def test_resolve_hands_whole_lines_to_the_reader() -> None:
    """The domain strips comments and blanks and nothing else. Choosing how to
    read an underscore needs the running engine's list format, so resolve must
    not split a line on the way past."""
    sf = SpeciesFilter(mode=FilterMode.LIST, list_text="Acoustic_guitar  # keep\n\n")
    seen: list[frozenset[str]] = []

    def record(lines: frozenset[str]) -> frozenset[str]:
        seen.append(lines)
        return lines

    sf.resolve([Path("a.wav")], _fake_region, record)

    assert seen == [frozenset({"Acoustic_guitar"})]
