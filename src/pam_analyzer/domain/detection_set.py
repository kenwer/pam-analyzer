"""Detections as persistable aggregates.

Detections are stored per model run in <campaign>/detections-<model_key>.csv.
A single Detection cannot save itself: rows share a file, and the file's
column order must survive a load/save round trip. DetectionFile owns one CSV
with its rows and column order. DetectionSet groups the files shown together
(one campaign, or the whole project) and routes edited rows back to the file
that owns them. Column names come from detection_schema. This module owns
only the file I/O.

The on-disk File column is campaign-relative. Load prepends the campaign
folder name so every in-memory consumer resolves against the project folder,
and write_detections_csv strips it again on save.
"""

import csv
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from . import detection_schema as schema
from . import paths
from .detection import Detection

_DTYPES = {c.name: pl.Float64 if c.numeric else pl.String for c in schema.COLUMNS}
_GETTERS = {c.name: c.get for c in schema.COLUMNS}


def _read_csv(path: Path) -> tuple[list[Detection], list[str]]:
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        detections = [schema.detection_from_row(row) for row in reader]
    return detections, fieldnames


def write_detections_csv(path: Path, detections: list[Detection], fieldnames: list[str]) -> None:
    """Write *detections* to *path* with *fieldnames* plus any missing annotation columns.

    The analysis runner and every save go through here, so a file keeps one
    format and a save without edits rewrites nothing. File is project-relative
    in memory but campaign-relative on disk, so the campaign prefix is
    stripped. Blank text is written as null, because polars writes an empty
    string as "" but a null as an empty cell.
    """
    columns = list(fieldnames) + [c for c in schema.ANNOTATION_COLUMNS if c not in fieldnames]
    data: dict[str, list] = {}
    for name in columns:
        get = _GETTERS.get(name)
        # A header column that is neither schema nor extra gets None, an empty cell.
        data[name] = [get(d) for d in detections] if get else [d.extra.get(name) for d in detections]
    dtypes = {name: _DTYPES.get(name, pl.String) for name in data}
    out = pl.DataFrame(data, schema=dtypes, strict=False)
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


@dataclass
class DetectionFile:
    """One detections CSV: its rows, its column order, and where it lives.

    fieldnames is the header as read, so a load/save round trip preserves the
    file's column order.
    """

    path: Path
    fieldnames: list[str]
    detections: list[Detection]

    @classmethod
    def load(cls, path: Path) -> DetectionFile:
        detections, fieldnames = _read_csv(path)
        campaign = path.parent.name
        for d in detections:
            if d.file and not Path(d.file).is_absolute():
                d.file = f"{campaign}/{d.file}"
        return cls(path, fieldnames, detections)

    def save(self) -> None:
        write_detections_csv(
            self.path,
            self.detections,
            self.fieldnames or list(schema.COLUMN_NAMES),
        )


@dataclass
class DetectionSet:
    """The detection files shown together: one campaign's, or a whole project's.

    detections is the flat concatenation of every file's rows, in file order.
    The Detection objects are shared with the files, so an edit made through
    the flat list is what the owning file writes on save.
    """

    files: list[DetectionFile]
    detections: list[Detection] = field(init=False)
    # Keyed by id() because Detection is a mutable, unhashable dataclass. The
    # files keep every row alive, so an id cannot be reused while it is here.
    _owner: dict[int, DetectionFile] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.detections = [d for f in self.files for d in f.detections]
        self._owner = {id(d): f for f in self.files for d in f.detections}

    @classmethod
    def load_for_campaign(cls, campaign_folder: Path) -> DetectionSet:
        return cls([DetectionFile.load(p) for p in schema.campaign_csvs(campaign_folder)])

    @classmethod
    def load_combined(cls, project_folder: Path) -> DetectionSet:
        """Concatenate every campaign's detection files into one aggregate.

        Each campaign CSV carries its own annotations, so the concatenation
        is always current. There is no combined file to fall out of sync.
        """
        return cls(
            [
                DetectionFile.load(p)
                for folder in paths.campaign_folders(project_folder)
                for p in schema.campaign_csvs(folder)
            ]
        )

    def save(self) -> None:
        """Write every file back to its CSV."""
        for f in self.files:
            f.save()

    def save_containing(self, rows: Iterable[Detection]) -> None:
        """Write only the files that own *rows*, each with its full row set.

        One edit in a 600k-row project then rewrites one CSV instead of all of
        them, which is what kept autosave from freezing the UI.
        """
        owners = {id(f): f for f in (self._owner[id(d)] for d in rows)}
        for f in owners.values():
            f.save()
