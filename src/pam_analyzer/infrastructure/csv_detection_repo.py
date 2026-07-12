"""Reads/writes detections CSVs. Drop-in compatible with the original column set.

Column names, row serialization, and the filename pattern all come from
domain.detection_schema; this module owns only the file I/O and the
routing of edits back to their source files.
"""

import csv
from pathlib import Path

from ..domain import Detection
from ..domain import detection_schema as schema
from . import paths


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
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=full_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(schema.detection_to_row(d) for d in detections)


class CsvDetectionRepository:
    """Reads and writes per-campaign detection CSVs.

    Each model run lands in its own file (<campaign>-detections-<model_key>.csv)
    so multiple runs can coexist for a campaign. Load enumerates every model
    file and concatenates them in memory. Save routes each detection back to
    the file it was loaded from via Detection.source_path. Per-file
    fieldnames are remembered so column order survives a load/save round
    trip.
    """

    def __init__(self) -> None:
        self._fieldnames_by_path: dict[Path, list[str]] = {}

    def load_for_campaign(self, output_base: Path, campaign_name: str) -> list[Detection]:
        all_detections: list[Detection] = []
        for path in paths.campaign_csvs(output_base, campaign_name):
            detections, fieldnames = _read_csv(path)
            self._fieldnames_by_path[path] = fieldnames
            all_detections.extend(detections)
        return all_detections

    def load_combined(self, output_base: Path) -> list[Detection]:
        """Concatenate every campaign's detections into one in-memory list.

        Each campaign CSV carries its own annotations, so the concatenation
        is always current; there is no combined file to fall out of sync.
        """
        all_detections: list[Detection] = []
        if not output_base.exists():
            return all_detections
        for sub in sorted(output_base.iterdir()):
            if not sub.is_dir():
                continue
            all_detections.extend(self.load_for_campaign(output_base, sub.name))
        return all_detections

    def save(self, detections: list[Detection]) -> None:
        """Write detections back to whichever CSV each one came from.

        load_for_campaign tags each row with its source path, so a campaign
        with both birdnet and perch runs round-trips correctly: each
        detection lands in the same file it came from.
        """
        groups: dict[Path, list[Detection]] = {}
        for d in detections:
            assert d.source_path is not None, "Detection must carry source_path when saved"
            groups.setdefault(d.source_path, []).append(d)
        for path, rows in groups.items():
            fieldnames = self._fieldnames_by_path.get(path) or list(schema.COLUMN_NAMES)
            _write_csv(path, rows, fieldnames)
