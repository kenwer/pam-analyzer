
import pytest

from pam_analyzer.domain import (
    LatLon,
    campaign_name_error,
)


def test_latlon_rejects_out_of_range():
    LatLon(0.0, 0.0)  # baseline OK
    with pytest.raises(ValueError):
        LatLon(91.0, 0.0)
    with pytest.raises(ValueError):
        LatLon(0.0, -181.0)


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
