"""A campaign's detections as a persistable aggregate.

Detections are stored per model run in <campaign>/detections-<model_key>.csv.
A single Detection cannot save itself: rows share a file, the file's column
order must survive a load/save round trip, and each row routes back to the
file it came from via Detection.source_path. DetectionSet is the unit that
owns those facts. Column names and row serialization come from
detection_schema; this module owns only the file I/O.

The on-disk File column is campaign-relative; load prepends the campaign
folder name so every in-memory consumer resolves against the project folder,
and _write_csv strips it again on save.
"""

import csv
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import detection_schema as schema
from . import paths
from .detection import Detection


def _read_csv(path: Path) -> tuple[list[Detection], list[str]]:
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        detections = []
        for row in reader:
            d = schema.detection_from_row(row)
            d.source_path = path
            detections.append(d)
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
class DetectionSet:
    """Detections loaded from a campaign or a whole project, plus enough state
    to write them back to the exact files they came from.

    fieldnames_by_path remembers each source file's column order so a
    load/save round trip preserves it.
    """

    detections: list[Detection]
    fieldnames_by_path: dict[Path, list[str]] = field(default_factory=dict)

    @classmethod
    def load_for_campaign(cls, campaign_folder: Path) -> "DetectionSet":
        ds = cls([])
        ds._extend_from_campaign(campaign_folder)
        return ds

    @classmethod
    def load_combined(cls, project_folder: Path) -> "DetectionSet":
        """Concatenate every campaign's detections into one aggregate.

        Each campaign CSV carries its own annotations, so the concatenation
        is always current; there is no combined file to fall out of sync.
        """
        ds = cls([])
        for folder in paths.campaign_folders(project_folder):
            ds._extend_from_campaign(folder)
        return ds

    def _extend_from_campaign(self, campaign_folder: Path) -> None:
        for path in schema.campaign_csvs(campaign_folder):
            detections, fieldnames = _read_csv(path)
            self.fieldnames_by_path[path] = fieldnames
            for d in detections:
                if d.file and not Path(d.file).is_absolute():
                    d.file = f"{campaign_folder.name}/{d.file}"
            self.detections.extend(detections)

    def save(self) -> None:
        """Write detections back to whichever CSV each one came from.

        Loading tags each row with its source path, so a campaign with both
        birdnet and perch runs round-trips correctly: each detection lands in
        the same file it came from.
        """
        groups: dict[Path, list[Detection]] = {}
        for d in self.detections:
            assert d.source_path is not None, "Detection must carry source_path when saved"
            groups.setdefault(d.source_path, []).append(d)
        for path, rows in groups.items():
            fieldnames = self.fieldnames_by_path.get(path) or list(schema.COLUMN_NAMES)
            _write_csv(path, rows, fieldnames)
