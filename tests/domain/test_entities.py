from pathlib import Path

import pytest

from pam_analyzer.domain import (
    Campaign,
    Detection,
    FilterMode,
    LatLon,
    Project,
    VerifiedState,
)


def test_project_default_output_base_uses_audio_root():
    project = Project(path=Path("/tmp/x.pamproj"), audio_recordings_path=Path("/tmp/audio"))
    assert project.output_base == Path("/tmp/audio/x-detections")


def test_project_explicit_output_base_wins():
    project = Project(
        path=Path("/tmp/x.pamproj"),
        audio_recordings_path=Path("/tmp/audio"),
        detections_output_path=Path("/elsewhere"),
    )
    assert project.output_base == Path("/elsewhere")


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
