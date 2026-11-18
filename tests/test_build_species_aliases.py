"""scripts/build_species_aliases.py derives the alias table from a taxonomy CSV."""

import pytest

from scripts.build_species_aliases import Bridge, derive_pairs, parse_supplement, parse_taxonomy

_HEADER = "scientific_name,taxon_group,scientific_name_aliases,ebird_code\n"


def _taxonomy(*rows: str):
    return parse_taxonomy(_HEADER + "".join(f"{r}\n" for r in rows))


def test_an_alias_maps_onto_its_accepted_name():
    taxonomy = _taxonomy("Astur gentilis,Aves,Accipiter gentilis,")

    pairs, unresolved = derive_pairs(taxonomy, {"v2.4": ["Accipiter gentilis", "Astur gentilis"]}, {})

    assert pairs == {"Accipiter gentilis": "Astur gentilis"}
    assert unresolved == {"v2.4": []}


def test_a_lump_maps_both_old_species_onto_one():
    taxonomy = _taxonomy("Acanthis flammea,Aves,Acanthis hornemanni|Acanthis cabaret,")

    pairs, _ = derive_pairs(taxonomy, {"v2.4": ["Acanthis flammea", "Acanthis hornemanni", "Acanthis cabaret"]}, {})

    assert pairs == {"Acanthis cabaret": "Acanthis flammea", "Acanthis hornemanni": "Acanthis flammea"}


def test_sound_class_aliases_are_search_terms_not_names():
    taxonomy = _taxonomy("Speech,Human,Human vocal,")

    pairs, unresolved = derive_pairs(taxonomy, {"v2.4": ["Human vocal"]}, {})

    assert pairs == {}
    assert unresolved == {"v2.4": ["Human vocal"]}


def test_the_supplement_bridges_and_overrides():
    taxonomy = _taxonomy("Grus virgo,Aves,,demcra1", "Iole finschii,Aves,Alophoixus finschii,")
    supplement = parse_supplement("# comment\nAnthropoides virgo\tGrus virgo\tdemcra1\nAlophoixus finschii\tIole finschii\n")

    pairs, unresolved = derive_pairs(taxonomy, {"v2.4": ["Anthropoides virgo", "Alophoixus finschii"]}, supplement)

    assert pairs == {"Alophoixus finschii": "Iole finschii", "Anthropoides virgo": "Grus virgo"}
    assert unresolved == {"v2.4": []}


def test_an_alias_naming_two_species_fails():
    with pytest.raises(SystemExit, match="more than one species"):
        _taxonomy("Aa bb,Aves,Old name,", "Cc dd,Aves,Old name,")


def test_a_chain_fails():
    taxonomy = _taxonomy("Bb bb,Aves,,")

    with pytest.raises(SystemExit, match="both superseded and canonical"):
        derive_pairs(taxonomy, {"x": ["Aa aa", "Bb bb"]}, {"Aa aa": Bridge("Bb bb"), "Bb bb": Bridge("Cc cc")})


def test_a_label_mapped_onto_itself_vetoes_the_taxonomy_alias():
    taxonomy = _taxonomy("Stenella frontalis,Mammalia,Bos frontalis,")

    pairs, unresolved = derive_pairs(taxonomy, {"perch": ["Bos frontalis"]}, {"Bos frontalis": Bridge("Bos frontalis")})

    assert pairs == {}
    assert unresolved == {"perch": []}


def test_a_bridge_whose_ebird_code_moved_fails():
    taxonomy = _taxonomy("Grus virgo,Aves,,othcra1")

    with pytest.raises(SystemExit, match="Anthropoides virgo"):
        derive_pairs(taxonomy, {"v2.4": []}, {"Anthropoides virgo": Bridge("Grus virgo", "demcra1")})


def test_a_supplement_line_without_a_tab_names_its_line_number():
    with pytest.raises(SystemExit, match="line 2"):
        parse_supplement("# comment\nAnthropoides virgo Grus virgo\n")
