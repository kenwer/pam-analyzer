from pathlib import Path

import pytest

from pam_analyzer.domain import (
    Campaign,
    Detection,
    FilterMode,
    LatLon,
    Project,
    VerifiedState,
    campaign_name_error,
)


def test_project_name_is_folder_name():
    project = Project(folder=Path("/tmp/My Project"))
    assert project.name == "My Project"


def test_latlon_rejects_out_of_range():
    LatLon(0.0, 0.0)  # baseline OK
    with pytest.raises(ValueError):
        LatLon(91.0, 0.0)
    with pytest.raises(ValueError):
        LatLon(0.0, -181.0)


def test_campaign_location_mode_holds_coordinates():
    c = Campaign(
        name="east",
        folder=Path("/audio/east"),
        species_filter_mode=FilterMode.LOCATION,
        location=LatLon(48.0, 11.0),
    )
    assert c.location is not None
    assert c.location.latitude == 48.0


def test_detection_default_annotations_are_unset():
    d = Detection(
        campaign="c",
        aru="a",
        week=1,
        species="Robin",
        scientific_name="Erithacus rubecula",
        confidence=0.8,
        start_time=0.0,
        end_time=3.0,
        rank=1,
        file="r.wav",
    )
    assert d.verified == VerifiedState.UNSET
    assert d.corrected_species == ""
    assert d.comment == ""
    assert d.extra == {}


def test_campaign_name_error_accepts_ordinary_names():
    assert campaign_name_error("Site A", ["other"]) is None
    # Glob characters are deliberately allowed (supported since 0.4.0).
    assert campaign_name_error("plot[1]*", []) is None


def test_campaign_name_error_rejects_empty_and_slashes():
    assert campaign_name_error("") is not None
    assert campaign_name_error("a/b") is not None
    assert campaign_name_error("a\\b") is not None


def test_campaign_name_error_rejects_windows_hostile_names():
    assert campaign_name_error("Site A.") is not None
    assert campaign_name_error("CON") is not None
    assert campaign_name_error("lpt3") is not None
    assert campaign_name_error("Nul.data") is not None


def test_campaign_name_error_detects_duplicates_across_normalization():
    import unicodedata

    nfd = unicodedata.normalize("NFD", "Süd")
    assert campaign_name_error("Süd", [nfd]) is not None
    assert campaign_name_error("Süd", ["other"]) is None
