"""Single definition of the Detection record's shape.

Owns the column names and their canonical order, per-column access, CSV
row serialization, and the detections CSV filename pattern. Every reader
and writer (CSV repository, analysis runners, Qt table model) derives
from this module, so a schema change lands in one place.

The canonical column order matches what the analysis runners write, so
an on-screen table built from COLUMNS is a direct visual analog of the
file on disk. Locale columns (Species_<locale>) are dynamic; they are
spliced in right after Species by write_fieldnames and are otherwise
carried in Detection.extra.

A Week value of -1 (audio_import.WEEK_YEAR_ROUND) marks a row whose
recording sat outside any week_NN folder and was therefore analyzed
against the year-round species list; the birdnet geo API itself uses -1
for "no week". An empty Week cell only occurs in files written by other
tools and simply means "unknown".
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .entities import Detection
from .enums import VerifiedState
from .filter_ops import ColumnKind


@dataclass(frozen=True)
class ColumnSpec:
    """One Detection column: identity plus typed access.

    set is None for read-only columns. annotation marks the user-editable
    review columns, which are appended on CSV writes even when the source
    file lacked them. kind drives the filter operator menu and matching
    semantics (see filter_ops.ColumnKind).
    """

    name: str
    get: Callable[[Detection], Any]
    set: Callable[[Detection, str], None] | None = None
    kind: ColumnKind = ColumnKind.TEXT
    annotation: bool = False

    @property
    def editable(self) -> bool:
        return self.set is not None

    @property
    def numeric(self) -> bool:
        return self.kind is ColumnKind.NUMERIC


def _set_verified(d: Detection, value: str) -> None:
    d.verified = VerifiedState(value or "")


def _set_corrected_species(d: Detection, value: str) -> None:
    d.corrected_species = value


def _set_comment(d: Detection, value: str) -> None:
    d.comment = value


_NUMERIC = ColumnKind.NUMERIC
_CATEGORICAL = ColumnKind.CATEGORICAL

COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("Campaign", lambda d: d.campaign, kind=_CATEGORICAL),
    ColumnSpec("ARU", lambda d: d.aru, kind=_CATEGORICAL),
    ColumnSpec("Start_Time", lambda d: d.start_time, kind=_NUMERIC),
    ColumnSpec("End_Time", lambda d: d.end_time, kind=_NUMERIC),
    ColumnSpec("Scientific_Name", lambda d: d.scientific_name),
    ColumnSpec("Species", lambda d: d.species, kind=_CATEGORICAL),
    ColumnSpec("Confidence", lambda d: d.confidence, kind=_NUMERIC),
    ColumnSpec("Rank", lambda d: d.rank, kind=_NUMERIC),
    ColumnSpec("File", lambda d: d.file),
    ColumnSpec("Recording_Time", lambda d: d.recording_time, kind=ColumnKind.DATETIME),
    ColumnSpec("Week", lambda d: d.week, kind=_NUMERIC),
    ColumnSpec("Lat", lambda d: d.lat, kind=_NUMERIC),
    ColumnSpec("Lon", lambda d: d.lon, kind=_NUMERIC),
    ColumnSpec("Species_List", lambda d: d.species_list, kind=_CATEGORICAL),
    ColumnSpec("Min_Conf", lambda d: d.min_conf, kind=_NUMERIC),
    ColumnSpec("Model", lambda d: d.model, kind=_CATEGORICAL),
    ColumnSpec("Verified", lambda d: d.verified.value, _set_verified, kind=_CATEGORICAL, annotation=True),
    ColumnSpec("Corrected_Species", lambda d: d.corrected_species, _set_corrected_species, kind=_CATEGORICAL, annotation=True),
    ColumnSpec("Comment", lambda d: d.comment, _set_comment, annotation=True),
)

COLUMN_NAMES: tuple[str, ...] = tuple(c.name for c in COLUMNS)

ANNOTATION_COLUMNS: tuple[str, ...] = tuple(c.name for c in COLUMNS if c.annotation)

# Membership set for splitting a CSV row into modeled fields vs Detection.extra.
CORE_FIELDS: frozenset[str] = frozenset(COLUMN_NAMES)

_LOCALE_COLUMN_PREFIX = "Species_"


def locale_column(locale: str) -> str:
    """Column name carrying common names for one locale (e.g. Species_de)."""
    return f"{_LOCALE_COLUMN_PREFIX}{locale}"


def is_locale_column(name: str) -> bool:
    return name.startswith(_LOCALE_COLUMN_PREFIX) and name not in CORE_FIELDS


def write_fieldnames(locales: Iterable[str] = ()) -> list[str]:
    """Canonical header for a freshly written detections CSV.

    Locale columns are spliced in right after Species, which is where
    users group them mentally with the base species name.
    """
    names = list(COLUMN_NAMES)
    species_pos = names.index("Species") + 1
    return [*names[:species_pos], *(locale_column(loc) for loc in locales), *names[species_pos:]]


def _to_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_optional_float(value: str) -> float | None:
    try:
        return float(value) if value not in ("", None) else None
    except (TypeError, ValueError):
        return None


def _format_number(value: float) -> str:
    """Render integers without a trailing .0; floats stay as float reprs."""
    if value == int(value):
        return str(int(value))
    return repr(value)


def detection_from_row(row: dict[str, str]) -> Detection:
    """Build a Detection from one CSV row; unknown columns land in extra."""
    return Detection(
        campaign=row.get("Campaign", ""),
        aru=row.get("ARU", ""),
        week=_to_optional_float(row.get("Week", "")),
        species=row.get("Species", ""),
        scientific_name=row.get("Scientific_Name", ""),
        confidence=_to_float(row.get("Confidence", "")),
        start_time=_to_float(row.get("Start_Time", "")),
        end_time=_to_float(row.get("End_Time", "")),
        rank=_to_optional_float(row.get("Rank", "")),
        file=row.get("File", ""),
        recording_time=row.get("Recording_Time", ""),
        lat=_to_optional_float(row.get("Lat", "")),
        lon=_to_optional_float(row.get("Lon", "")),
        species_list=row.get("Species_List", ""),
        min_conf=_to_optional_float(row.get("Min_Conf", "")),
        model=row.get("Model", ""),
        verified=VerifiedState(row.get("Verified", "") or ""),
        corrected_species=row.get("Corrected_Species", ""),
        comment=row.get("Comment", ""),
        extra={k: v for k, v in row.items() if k not in CORE_FIELDS},
    )


def detection_to_row(d: Detection) -> dict[str, str]:
    """Serialize a Detection to CSV string values, extra columns included."""
    row: dict[str, str] = dict(d.extra)
    row["Campaign"] = d.campaign
    row["ARU"] = d.aru
    row["Week"] = "" if d.week is None else _format_number(d.week)
    row["Species"] = d.species
    row["Scientific_Name"] = d.scientific_name
    row["Confidence"] = _format_number(d.confidence)
    row["Start_Time"] = _format_number(d.start_time)
    row["End_Time"] = _format_number(d.end_time)
    row["Rank"] = "" if d.rank is None else _format_number(d.rank)
    row["File"] = d.file
    row["Recording_Time"] = d.recording_time
    row["Lat"] = "" if d.lat is None else _format_number(d.lat)
    row["Lon"] = "" if d.lon is None else _format_number(d.lon)
    row["Species_List"] = d.species_list
    row["Min_Conf"] = "" if d.min_conf is None else _format_number(d.min_conf)
    row["Model"] = d.model
    row["Verified"] = d.verified.value
    row["Corrected_Species"] = d.corrected_species
    row["Comment"] = d.comment
    return row


_DETECTIONS_PREFIX = "detections-"
_CSV_SUFFIX = ".csv"


def detections_csv_name(model_key: str) -> str:
    """Filename of a campaign's detections CSV for one model run.

    Deliberately free of the campaign name so renaming a campaign folder
    never invalidates its CSVs.
    """
    return f"{_DETECTIONS_PREFIX}{model_key}{_CSV_SUFFIX}"


def model_key_from_csv_name(filename: str) -> str | None:
    """Inverse of detections_csv_name; None when the name doesn't match."""
    if not (filename.startswith(_DETECTIONS_PREFIX) and filename.endswith(_CSV_SUFFIX)):
        return None
    return filename[len(_DETECTIONS_PREFIX) : -len(_CSV_SUFFIX)]
