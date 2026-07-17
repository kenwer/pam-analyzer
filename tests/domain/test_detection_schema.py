"""Tests for the detection schema: the single definition of the Detection
record's columns, serialization, and filename pattern."""

from pam_analyzer.domain import Detection, VerifiedState
from pam_analyzer.domain import detection_schema as schema


def _sample_detection() -> Detection:
    return Detection(
        campaign="Camp-A",
        aru="MSD-109",
        week=8.0,
        species="Eurasian Blackbird",
        scientific_name="Turdus merula",
        confidence=0.8534,
        start_time=12.0,
        end_time=15.0,
        rank=1.0,
        file="Camp-A/MSD-109/week_08/rec.flac",
        recording_time="2026-03-26 06:00:00",
        lat=47.94,
        lon=9.32,
        species_list="",
        min_conf=0.25,
        model="BirdNET-2.4",
        verified=VerifiedState.TRUE,
        corrected_species="",
        comment="clear song",
        extra={"Species_de": "Amsel"},
    )


class TestColumns:
    def test_annotation_columns_derived_from_specs(self):
        assert schema.ANNOTATION_COLUMNS == ("Verified", "Corrected_Species", "Comment")

    def test_column_names_are_unique(self):
        assert len(set(schema.COLUMN_NAMES)) == len(schema.COLUMN_NAMES)

    def test_getters_cover_every_column(self):
        d = _sample_detection()
        values = {c.name: c.get(d) for c in schema.COLUMNS}
        assert values["Campaign"] == "Camp-A"
        assert values["Confidence"] == 0.8534
        assert values["Verified"] == "true"  # getter unwraps the enum

    def test_setters_only_on_annotation_columns(self):
        editable = {c.name for c in schema.COLUMNS if c.editable}
        assert editable == set(schema.ANNOTATION_COLUMNS)

    def test_setters_mutate_detection(self):
        d = _sample_detection()
        by_name = {c.name: c for c in schema.COLUMNS}
        by_name["Verified"].set(d, "uncertain")
        by_name["Corrected_Species"].set(d, "Turdus pilaris")
        by_name["Comment"].set(d, "second opinion")
        assert d.verified is VerifiedState.UNCERTAIN
        assert d.corrected_species == "Turdus pilaris"
        assert d.comment == "second opinion"


class TestWriteFieldnames:
    def test_no_locales_matches_column_names(self):
        assert schema.write_fieldnames() == list(schema.COLUMN_NAMES)

    def test_locale_columns_spliced_after_species(self):
        names = schema.write_fieldnames(["de", "fr"])
        species_pos = names.index("Species")
        assert names[species_pos + 1 : species_pos + 3] == ["Species_de", "Species_fr"]
        # Removing the locale columns restores the canonical order.
        assert [n for n in names if not schema.is_locale_column(n)] == list(schema.COLUMN_NAMES)
        assert names[-3:] == list(schema.ANNOTATION_COLUMNS)


class TestRowRoundTrip:
    def test_round_trip_preserves_fields_and_extras(self):
        d = _sample_detection()
        row = schema.detection_to_row(d)
        back = schema.detection_from_row(row)
        assert back.campaign == d.campaign
        assert back.confidence == d.confidence
        assert back.week == d.week
        assert back.verified is VerifiedState.TRUE
        assert back.comment == "clear song"
        assert back.extra == {"Species_de": "Amsel"}

    def test_integers_written_without_trailing_zero(self):
        row = schema.detection_to_row(_sample_detection())
        assert row["Week"] == "8"
        assert row["Start_Time"] == "12"
        assert row["Rank"] == "1"
        assert row["Confidence"] == "0.8534"

    def test_optional_fields_written_empty_and_read_as_none(self):
        d = _sample_detection()
        d.week = None
        d.rank = None
        d.lat = None
        d.lon = None
        d.min_conf = None
        row = schema.detection_to_row(d)
        assert row["Week"] == "" and row["Rank"] == "" and row["Lat"] == ""
        back = schema.detection_from_row(row)
        assert back.week is None and back.rank is None and back.lat is None

    def test_unknown_columns_land_in_extra(self):
        row = {"Campaign": "C", "Species": "X", "Some_Future_Column": "42"}
        d = schema.detection_from_row(row)
        assert d.extra == {"Some_Future_Column": "42"}


class TestFilenamePattern:
    def test_build_and_parse_are_inverse(self):
        name = schema.detections_csv_name("Perch-2.0")
        assert name == "detections-Perch-2.0.csv"
        assert schema.model_key_from_csv_name(name) == "Perch-2.0"

    def test_parse_rejects_foreign_names(self):
        assert schema.model_key_from_csv_name("detections.csv") is None
        assert schema.model_key_from_csv_name("Camp-A-detections-x.csv") is None
        assert schema.model_key_from_csv_name("applied-species-list.txt") is None

    def test_locale_column_helpers(self):
        assert schema.locale_column("de") == "Species_de"
        assert schema.is_locale_column("Species_de")
        # Species and Species_List are core columns, not locale columns.
        assert not schema.is_locale_column("Species")
        assert not schema.is_locale_column("Species_List")
