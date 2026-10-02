"""All loaded detections as one polars frame, plus the files they came from.

The frame is the single in-memory copy: the Examine table reads cells from
it, filters and sorts on it, edits land in it, and saving writes slices of
it back to the CSV each row came from. A row id is a row's position in the
frame. It is stable for the store's lifetime because rows are never added,
removed or reordered, but a new store numbers its rows from 0 again.

Reading parses cells the way detection_schema.detection_from_row does.
"""

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from . import detection_schema as schema
from . import paths
from .detection import Detection
from .enums import VerifiedState

_VERIFIED_VALUES = [v.value for v in VerifiedState]
# Mirrors Path.is_absolute on this platform. On Windows that means a drive
# with a root, or a UNC share, and a bare leading slash is not absolute.
_ABSOLUTE_FILE = r"^([A-Za-z]:[\\/]|[\\/]{2})" if os.name == "nt" else r"^/"


def empty_detections_frame() -> pl.DataFrame:
    """Zero-row frame with every schema column, typed as in FRAME_DTYPES."""
    return pl.DataFrame(schema=schema.FRAME_DTYPES)


def read_detections_csv(path: Path) -> tuple[pl.DataFrame, list[str]]:
    """Typed frame and header of one detections CSV.

    Blank or unparsable numbers become 0.0 in NUMERIC_DEFAULT_ZERO columns
    and null elsewhere. An unknown Verified value raises ValueError. Schema
    columns the file lacks are added, blank. File gains the campaign folder
    prefix so it resolves against the project folder.
    """
    if path.stat().st_size == 0:
        return empty_detections_frame(), []
    # All columns as String, so parsing below controls every conversion.
    raw = pl.read_csv(path, infer_schema_length=0, truncate_ragged_lines=True).fill_null("")
    fieldnames = raw.columns
    raw = raw.with_columns(
        [pl.lit("").alias(c.name) for c in schema.COLUMNS if c.name not in raw.columns]
    )
    numeric = []
    for c in schema.COLUMNS:
        if c.numeric:
            parsed = pl.col(c.name).str.strip_chars().cast(pl.Float64, strict=False)
            if c.name in schema.NUMERIC_DEFAULT_ZERO:
                parsed = parsed.fill_null(0.0)
            numeric.append(parsed.alias(c.name))
    frame = raw.with_columns(numeric)
    invalid = frame.filter(~pl.col("Verified").is_in(_VERIFIED_VALUES))["Verified"]
    if not invalid.is_empty():
        raise ValueError(f"{path}: {invalid[0]!r} is not a valid Verified value")
    file = pl.col("File")
    prefixed = pl.concat_str(pl.lit(path.parent.name + "/"), file)
    frame = frame.with_columns(
        pl.when((file != "") & ~file.str.contains(_ABSOLUTE_FILE))
        .then(prefixed)
        .otherwise(file)
        .alias("File")
    )
    return frame, fieldnames


_GETTERS = {c.name: c.get for c in schema.COLUMNS}


def _header(fieldnames: list[str]) -> list[str]:
    """*fieldnames* plus any missing annotation columns, each name once."""
    return list(dict.fromkeys([*fieldnames, *schema.ANNOTATION_COLUMNS]))


def write_detections_csv(path: Path, detections: list[Detection], fieldnames: list[str]) -> None:
    """Write *detections* to *path*. The analysis runner's entry point.

    Builds the typed frame write_detections_frame expects, so rows from the
    runner and rows from a DetectionStore come out in the same format.
    """
    data: dict[str, list] = {}
    for name in _header(fieldnames):
        get = _GETTERS.get(name)
        # A header column that is neither schema nor extra gets None, an empty cell.
        data[name] = [get(d) for d in detections] if get else [d.extra.get(name) for d in detections]
    # Explicit, so the runner's ints and a loaded file's floats both write as 1.0.
    dtypes = {name: schema.FRAME_DTYPES.get(name, pl.String) for name in data}
    write_detections_frame(path, pl.DataFrame(data, schema=dtypes, strict=False), fieldnames)


def write_detections_frame(path: Path, frame: pl.DataFrame, fieldnames: list[str]) -> None:
    """Write *frame* to *path* with *fieldnames* plus any missing annotation columns.

    The analysis runner and every save end up here, so a file keeps one format
    and a save without edits rewrites nothing. File is project-relative in
    memory but campaign-relative on disk, so the campaign prefix is stripped.
    Blank text is written as null, because polars writes an empty string as ""
    but a null as an empty cell.
    """
    out = frame.select(_header(fieldnames))
    if "File" in out.columns:
        out = out.with_columns(pl.col("File").str.strip_prefix(path.parent.name + "/"))
    out = out.with_columns(pl.col(pl.String).replace("", None))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    try:
        out.write_csv(tmp, line_terminator="\r\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


_ANNOTATIONS = {c.name: c for c in schema.COLUMNS if c.annotation}


@dataclass(frozen=True)
class SourceFile:
    """One detections CSV: where it lives and its header as read."""

    path: Path
    fieldnames: list[str]


class DetectionStore:
    """Detections from one campaign or a whole project, editable and savable."""

    def __init__(self, frame: pl.DataFrame, files: list[SourceFile], file_index: pl.Series) -> None:
        self._frame = frame
        self.files = files
        # Position in files of the CSV each row came from, row-aligned with the frame.
        self._file_index = file_index
        self._dirty: set[int] = set()

    @classmethod
    def empty(cls) -> DetectionStore:
        return cls(empty_detections_frame(), [], pl.Series(dtype=pl.UInt32))

    @classmethod
    def from_paths(cls, csv_paths: Iterable[Path]) -> DetectionStore:
        frames: list[pl.DataFrame] = []
        files: list[SourceFile] = []
        index: list[pl.Series] = []
        for i, path in enumerate(csv_paths):
            frame, fieldnames = read_detections_csv(path)
            frames.append(frame)
            files.append(SourceFile(path, fieldnames))
            index.append(pl.repeat(i, frame.height, dtype=pl.UInt32, eager=True))
        if not frames:
            return cls.empty()
        # diagonal_relaxed fills an extra column that only some files have with null.
        # Rechunked because every per-file save filters the whole frame, which
        # took seconds over the thousands of chunks read_csv returns.
        frame = pl.concat(frames, how="diagonal_relaxed", rechunk=True)
        return cls(frame, files, pl.concat(index, rechunk=True))

    @classmethod
    def load_for_campaign(cls, campaign_folder: Path) -> DetectionStore:
        return cls.from_paths(schema.campaign_csvs(campaign_folder))

    @classmethod
    def load_combined(cls, project_folder: Path) -> DetectionStore:
        """Every campaign's CSVs in one store.

        Each campaign CSV carries its own annotations, so the concatenation is
        always current. There is no combined file to fall out of sync.
        """
        return cls.from_paths(
            p for folder in paths.campaign_folders(project_folder) for p in schema.campaign_csvs(folder)
        )

    @property
    def frame(self) -> pl.DataFrame:
        """The current frame. Treat it as read-only and edit through set_annotation."""
        return self._frame

    @property
    def row_count(self) -> int:
        return self._frame.height

    @property
    def dirty_count(self) -> int:
        return len(self._dirty)

    def detection(self, row_id: int) -> Detection:
        """A snapshot of one row. Changing it does not change the store."""
        return schema.detection_from_record(self._frame.row(row_id, named=True))

    def set_annotation(self, row_id: int, column: str, value: str) -> None:
        """Set one annotation cell and mark its row dirty. See set_annotations."""
        self.set_annotations([row_id], column, value)

    def set_annotations(self, row_ids: Sequence[int], column: str, value: str) -> None:
        """Set one annotation column to *value* on every row in *row_ids* and mark them dirty.

        Raises ValueError for a column that is not an annotation column, or a
        value the column's parser rejects (an unknown Verified state).
        """
        spec = _ANNOTATIONS.get(column)
        if spec is None:
            raise ValueError(f"{column} is not an editable column")
        # VerifiedState is a StrEnum, so str() yields its CSV value.
        text = str(spec.parse(value))
        # One scatter for all rows, because each scatter copies the whole column.
        self._frame = self._frame.with_columns(self._frame.get_column(column).scatter(row_ids, text))
        self._dirty.update(row_ids)

    def save(self) -> None:
        """Write every file back to its CSV."""
        for i in range(len(self.files)):
            self._save_file(i)
        self._dirty.clear()

    def save_dirty(self) -> int:
        """Write only the files holding edited rows. Returns how many rows were saved.

        Rows stay dirty when a write fails, so the next save retries them.
        """
        if not self._dirty:
            return 0
        owners = self._file_index.gather(sorted(self._dirty)).unique().sort().to_list()
        for i in owners:
            self._save_file(i)
        saved = len(self._dirty)
        self._dirty.clear()
        return saved

    def discard_dirty(self) -> None:
        self._dirty.clear()

    def _save_file(self, i: int) -> None:
        f = self.files[i]
        write_detections_frame(
            f.path,
            self._frame.filter(self._file_index == i),
            f.fieldnames or list(schema.COLUMN_NAMES),
        )
