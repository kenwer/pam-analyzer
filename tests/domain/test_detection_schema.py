"""Tests for the detection schema: the single definition of the Detection
record's columns, serialization, and filename pattern."""

from dataclasses import fields

from pam_analyzer.domain import Detection, VerifiedState
from pam_analyzer.domain import detection_schema as schema
from tests.conftest import DEFAULT_MODEL_KEY


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
        model=DEFAULT_MODEL_KEY,
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

    def test_columns_cover_every_detection_field(self):
        """Serialization derives from COLUMNS, so a Detection field without a
        column would silently vanish from the CSV. Only extra, which carries
        its own unmodeled columns, may be missing."""
        attrs = {c.attr for c in schema.COLUMNS}
        field_names = {f.name for f in fields(Detection)}
        assert field_names - attrs == {"extra"}
        assert attrs <= field_names

    def test_setters_mutate_detection(self):
        d = _sample_detection()
        by_name = {c.name: c for c in schema.COLUMNS}
        for name, value in [("Verified", "uncertain"), ("Corrected_Species", "Turdus pilaris"), ("Comment", "second opinion")]:
            setter = by_name[name].set
            assert setter is not None
            setter(d, value)
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


class TestFilenamePattern:
    def test_build_and_parse_are_inverse(self):
        # A key the app no longer writes, on purpose: round-tripping any key
        # is what keeps CSVs from retired models readable.
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
