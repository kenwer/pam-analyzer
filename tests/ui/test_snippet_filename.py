"""Tests for the file names of exported audio snippets."""

import unicodedata
from dataclasses import replace

import pytest

from pam_analyzer.domain import Detection
from pam_analyzer.domain.enums import VerifiedState
from pam_analyzer.ui.panels.examine_panel import _snippet_filename, _unique_snippet_filename

_BASE = "CmpRot2_Zollhauser-Bach-Riedlingen__ID_106__Long-eared_Owl__20260501_053000__12.0-15.0__BirdNET-2.4__conf0.87"


def _detection(**overrides) -> Detection:
    detection = Detection(
        campaign="CmpRot2_Zollhauser-Bach-Riedlingen",
        aru="ID 106",
        week=None,
        species="Long-eared Owl",
        scientific_name="Asio otus",
        confidence=0.8731,
        start_time=12.0,
        end_time=15.0,
        rank=None,
        file="ID 106/week_19/rec.flac",
        recording_time="2026-05-01 05:30:00",
        model="BirdNET-2.4",
    )
    return replace(detection, **overrides)


def _name(**overrides) -> str:
    return _snippet_filename(_detection(**overrides), 12.0, 15.0)


def _species_field(species: str) -> str:
    return _name(species=species).split("__")[2]


def test_unannotated_detection_has_seven_fields() -> None:
    assert _name() == _BASE + ".flac"


def test_range_uses_the_padded_start_and_end() -> None:
    name = _snippet_filename(_detection(), 11.5, 16.0)
    assert name.split("__")[4] == "11.5-16.0"


@pytest.mark.parametrize(
    ("verified", "status"),
    [
        (VerifiedState.TRUE, "confirmed"),
        (VerifiedState.FALSE, "incorrect"),
        (VerifiedState.UNCERTAIN, "uncertain"),
    ],
)
def test_verified_adds_a_status(verified: VerifiedState, status: str) -> None:
    assert _name(verified=verified) == f"{_BASE}__{status}.flac"


@pytest.mark.parametrize("verified", [VerifiedState.UNSET, VerifiedState.TRUE, VerifiedState.FALSE])
def test_correction_replaces_species_and_status(verified: VerifiedState) -> None:
    name = _name(verified=verified, corrected_species="Tawny Owl")
    assert name == _BASE.replace("Long-eared_Owl", "Tawny_Owl") + "__corrected.flac"


def test_uncertain_correction_keeps_the_doubt() -> None:
    name = _name(verified=VerifiedState.UNCERTAIN, corrected_species="Tawny Owl")
    assert name.endswith("__Tawny_Owl__20260501_053000__12.0-15.0__BirdNET-2.4__conf0.87__corrected_uncertain.flac")


def test_comment_is_added_after_the_status() -> None:
    name = _name(verified=VerifiedState.TRUE, comment="two birds")
    assert name == _BASE + "__confirmed__comment_two_birds.flac"


def test_comment_without_status() -> None:
    assert _name(comment="two birds") == _BASE + "__comment_two_birds.flac"


def test_comment_is_cut_to_20_characters() -> None:
    name = _name(comment="distant, two birds calling at once")
    assert name == _BASE + "__comment_distant_two_birds_ca.flac"


def test_cut_comment_does_not_end_in_an_underscore() -> None:
    name = _name(comment="aaaa bbbb cccc dddd eeee")
    assert name == _BASE + "__comment_aaaa_bbbb_cccc_dddd.flac"


def test_comment_without_usable_characters_is_left_out() -> None:
    assert _name(comment=" ?! ") == _BASE + ".flac"


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("Long-eared Owl", "Long-eared_Owl"),
        ("Mönchsgrasmücke", "Mönchsgrasmücke"),
        ("シジュウカラ", "シジュウカラ"),
        ("Bonelli's Warbler", "Bonellis_Warbler"),
        ("Bonelli’s Warbler", "Bonellis_Warbler"),
        ("Accipiter gentilis/nisus", "Accipiter_gentilis_nisus"),
        ('a<b>:"c|d?*e\\f', "a_b_c_d_e_f"),
        ("tab\tand\nnewline", "tab_and_newline"),
        ("Carduelis sp.", "Carduelis_sp"),
        ("  padded  ", "padded"),
    ],
)
def test_text_fields_are_made_safe(raw: str, safe: str) -> None:
    assert _species_field(raw) == safe


def test_decomposed_umlaut_is_composed_instead_of_split() -> None:
    campaign = unicodedata.normalize("NFD", "CmpRot2_Löffingen")
    assert _name(campaign=campaign).split("__")[0] == "CmpRot2_Löffingen"


@pytest.mark.parametrize("campaign", ["a__b", "a _ b", "__a_b__", "a___b"])
def test_field_never_contains_the_separator(campaign: str) -> None:
    assert _name(campaign=campaign).split("__")[0] == "a_b"


def test_missing_text_fields_fall_back_to_unknown() -> None:
    name = _name(campaign="", aru="??", species="", scientific_name="")
    assert name.split("__")[:3] == ["unknown", "unknown", "unknown"]


def test_species_falls_back_to_scientific_name() -> None:
    assert _species_field("") == "Asio_otus"


def test_unparsable_recording_time_is_made_safe() -> None:
    assert _name(recording_time="20/05/2026 10:00").split("__")[3] == "20_05_2026_10_00"


def test_missing_recording_time() -> None:
    assert _name(recording_time="").split("__")[3] == "unknown_time"


def test_model_tells_apart_equal_detections_of_two_models() -> None:
    assert _name(model="BirdNET-2.4") != _name(model="Perch-2.0")
    assert _name(model="Perch-2.0").split("__")[5] == "Perch-2.0"


def test_missing_model_falls_back_to_unknown() -> None:
    assert _name(model="").split("__")[5] == "unknown"


def test_unique_name_is_unchanged_on_first_use() -> None:
    used: set[str] = set()
    assert _unique_snippet_filename("a__b.flac", used) == "a__b.flac"


def test_repeated_name_gets_a_counter() -> None:
    used: set[str] = set()
    names = [_unique_snippet_filename("a__b.flac", used) for _ in range(3)]
    assert names == ["a__b.flac", "a__b__2.flac", "a__b__3.flac"]


def test_counter_skips_a_name_already_taken() -> None:
    used = {"a__b__2.flac"}
    assert _unique_snippet_filename("a__b.flac", used) == "a__b.flac"
    assert _unique_snippet_filename("a__b.flac", used) == "a__b__3.flac"
