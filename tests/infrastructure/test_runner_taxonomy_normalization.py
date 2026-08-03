"""How the runners normalize a renamed bird's output axis in _parse_row.

_parse_row is pure (no model load), so both runners are exercised directly with
a synthetic result row. The bundled crosswalk carries Accipiter gentilis <->
Astur gentilis, which these tests rely on.
"""

from pam_analyzer.domain import AnalysisSettings
from pam_analyzer.infrastructure.birdnet_runner import BirdnetRunner
from pam_analyzer.infrastructure.perch_runner import PerchRunner

# Common-name maps are keyed on BirdNET scientific names.
_PREFERRED = {"Accipiter gentilis": "Northern Goshawk"}
_LOCALES = {"de": {"Accipiter gentilis": "Habicht"}}


def _perch_row(sci: str) -> dict:
    # Perch's raw logit 11.2 is the offset, which maps to ~0.5 probability.
    return {
        "species_name": sci,
        "confidence": 11.2,
        "input": "/x/a.wav",
        "start_time": 0.0,
        "end_time": 5.0,
    }


def test_perch_renamed_bird_normalizes_to_birdnet_axis_by_default() -> None:
    settings = AnalysisSettings(locales=("de",), canonical_taxonomy="BirdNET-2.4")
    parsed = PerchRunner()._parse_row(
        _perch_row("Astur gentilis"),
        preferred_lang_map=_PREFERRED,
        locale_maps=_LOCALES,
        settings=settings,
    )
    # Output name is the BirdNET spelling; the filter still sees the native one.
    assert parsed.scientific_name == "Accipiter gentilis"
    assert parsed.match_name == "Astur gentilis"
    # Common names fill in because the lookup routes through the BirdNET name.
    assert parsed.preferred_common == "Northern Goshawk"
    assert parsed.locale_commons["de"] == "Habicht"


def test_perch_renamed_bird_keeps_common_name_under_perch_axis() -> None:
    settings = AnalysisSettings(locales=("de",), canonical_taxonomy="Perch-2.0")
    parsed = PerchRunner()._parse_row(
        _perch_row("Astur gentilis"),
        preferred_lang_map=_PREFERRED,
        locale_maps=_LOCALES,
        settings=settings,
    )
    # Output keeps the Perch spelling, but the common name still resolves.
    assert parsed.scientific_name == "Astur gentilis"
    assert parsed.match_name == "Astur gentilis"
    assert parsed.preferred_common == "Northern Goshawk"


def test_birdnet_output_axis_follows_canonical_setting() -> None:
    row = {
        "species_name": "Accipiter gentilis_Northern Goshawk",
        "confidence": 0.9,
        "input": "/x/a.wav",
        "start_time": 0.0,
        "end_time": 3.0,
    }
    # Default axis is a no-op for BirdNET names.
    default = BirdnetRunner()._parse_row(
        row, preferred_lang_map={}, locale_maps={}, settings=AnalysisSettings()
    )
    assert default.scientific_name == "Accipiter gentilis"
    assert default.match_name == "Accipiter gentilis"

    # Under the Perch axis the written name flips, but the filter name does not.
    perch_axis = BirdnetRunner()._parse_row(
        row,
        preferred_lang_map={},
        locale_maps={},
        settings=AnalysisSettings(canonical_taxonomy="Perch-2.0"),
    )
    assert perch_axis.scientific_name == "Astur gentilis"
    assert perch_axis.match_name == "Accipiter gentilis"
