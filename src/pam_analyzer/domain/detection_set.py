"""Detections as persistable aggregates.

Detections are stored per model run in <campaign>/detections-<model_key>.csv.
A single Detection cannot save itself: rows share a file, and the file's
column order must survive a load/save round trip. DetectionFile owns one CSV
with its rows and column order. DetectionSet groups the files shown together
(one campaign, or the whole project) and routes edited rows back to the file
that owns them. Column names and row serialization come from
detection_schema. This module owns only the file I/O.

The on-disk File column is campaign-relative. Load prepends the campaign
folder name so every in-memory consumer resolves against the project folder,
and _write_csv strips it again on save.
"""

import csv
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import detection_schema as schema
from . import paths
from .detection import Detection


def _read_csv(path: Path) -> tuple[list[Detection], list[str]]:
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        detections = [schema.detection_from_row(row) for row in reader]
    return detections, fieldnames


def _write_csv(path: Path, detections: list[Detection], fieldnames: list[str]) -> None:
    full_fields = list(fieldnames)
    for f in schema.ANNOTATION_COLUMNS:
        if f not in full_fields:
            full_fields.append(f)
    path.parent.mkdir(parents=True, exist_ok=True)
    # On disk the File column is campaign-relative so a campaign folder can be
    # renamed or moved without breaking its CSVs. In memory it is
    # project-relative (load prepends the folder name), so strip the prefix
    # from the row dict here, never from the shared Detection.
    campaign_prefix = path.parent.name + "/"

    def _row(d: Detection) -> dict[str, str]:
        row = schema.detection_to_row(d)
        if row["File"].startswith(campaign_prefix):
            row["File"] = row["File"][len(campaign_prefix):]
        return row

    # Write to a sibling temp file and swap it in atomically: this CSV holds
    # the user's annotations, so a crash mid-write must not truncate the only
    # copy. The '.part' suffix keeps discovery globs from matching the temp.
    tmp = path.with_name(path.name + ".part")
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=full_fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(_row(d) for d in detections)
        os.replace(tmp, path)
    finally:
        # On success os.replace consumed tmp; on any failure discard the partial.
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
        _write_csv(self.path, self.detections, self.fieldnames or list(schema.COLUMN_NAMES))


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
