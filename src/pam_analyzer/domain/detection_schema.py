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
from operator import attrgetter
from typing import Any

from .entities import Detection
from .enums import VerifiedState
from .filter_ops import ColumnKind


@dataclass(frozen=True)
class ColumnSpec:
    """One Detection column: identity, typed access, and CSV conversion.

    set is None for read-only columns. annotation marks the user-editable
    review columns, which are appended on CSV writes even when the source
    file lacked them. kind drives the filter operator menu and matching
    semantics (see filter_ops.ColumnKind), plus numeric cell formatting
    on CSV writes.

    attr and parse only matter for the schema columns in COLUMNS: attr
    names the Detection attribute behind the column, and parse turns a
    CSV cell into that attribute's value. Build schema columns through
    _column, which derives get and set from them so each fact is stated
    once. Display-only columns (the play column, locale extras) are
    constructed directly and leave both at their defaults.
    """

    name: str
    get: Callable[[Detection], Any]
    set: Callable[[Detection, str], None] | None = None
    kind: ColumnKind = ColumnKind.TEXT
    annotation: bool = False
    attr: str = ""
    parse: Callable[[str], Any] = str

    @property
    def editable(self) -> bool:
        return self.set is not None

    @property
    def numeric(self) -> bool:
        return self.kind is ColumnKind.NUMERIC


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


def _parse_verified(value: str) -> VerifiedState:
    return VerifiedState(value or "")


def _column(
    name: str,
    attr: str,
    *,
    parse: Callable[[str], Any] = str,
    kind: ColumnKind = ColumnKind.TEXT,
    annotation: bool = False,
    get: Callable[[Detection], Any] | None = None,
) -> ColumnSpec:
    """Build a schema column, deriving get and set from attr and parse.

    get is overridable for columns whose serialized value is not the raw
    attribute (Verified exposes the enum's CSV string).
    """
    if get is None:
        get = attrgetter(attr)
    if annotation:
        def set_(d: Detection, value: str) -> None:
            setattr(d, attr, parse(value))
    else:
        set_ = None
    return ColumnSpec(name, get, set_, kind, annotation, attr, parse)


_NUMERIC = ColumnKind.NUMERIC
_CATEGORICAL = ColumnKind.CATEGORICAL

COLUMNS: tuple[ColumnSpec, ...] = (
    _column("Campaign", "campaign", kind=_CATEGORICAL),
    _column("ARU", "aru", kind=_CATEGORICAL),
    _column("Start_Time", "start_time", parse=_to_float, kind=_NUMERIC),
    _column("End_Time", "end_time", parse=_to_float, kind=_NUMERIC),
    _column("Scientific_Name", "scientific_name"),
    _column("Species", "species", kind=_CATEGORICAL),
    _column("Confidence", "confidence", parse=_to_float, kind=_NUMERIC),
    _column("Rank", "rank", parse=_to_optional_float, kind=_NUMERIC),
    _column("File", "file"),
    _column("Recording_Time", "recording_time", kind=ColumnKind.DATETIME),
    _column("Week", "week", parse=_to_optional_float, kind=_NUMERIC),
    _column("Lat", "lat", parse=_to_optional_float, kind=_NUMERIC),
    _column("Lon", "lon", parse=_to_optional_float, kind=_NUMERIC),
    _column("Species_List", "species_list", kind=_CATEGORICAL),
    _column("Min_Conf", "min_conf", parse=_to_optional_float, kind=_NUMERIC),
    _column("Model", "model", kind=_CATEGORICAL),
    _column("Verified", "verified", parse=_parse_verified, kind=_CATEGORICAL,
            annotation=True, get=lambda d: d.verified.value),
    _column("Corrected_Species", "corrected_species", kind=_CATEGORICAL, annotation=True),
    _column("Comment", "comment", annotation=True),
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


def _format_number(value: float) -> str:
    """Render integers without a trailing .0; floats stay as float reprs."""
    if value == int(value):
        return str(int(value))
    return repr(value)


def _format_cell(column: ColumnSpec, d: Detection) -> str:
    value = column.get(d)
    if column.numeric:
        return "" if value is None else _format_number(value)
    return str(value)


def detection_from_row(row: dict[str, str]) -> Detection:
    """Build a Detection from one CSV row; unknown columns land in extra."""
    kwargs: dict[str, Any] = {c.attr: c.parse(row.get(c.name, "")) for c in COLUMNS}
    kwargs["extra"] = {k: v for k, v in row.items() if k not in CORE_FIELDS}
    return Detection(**kwargs)


def detection_to_row(d: Detection) -> dict[str, str]:
    """Serialize a Detection to CSV string values, extra columns included."""
    row: dict[str, str] = dict(d.extra)
    row.update({c.name: _format_cell(c, d) for c in COLUMNS})
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
