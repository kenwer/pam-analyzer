"""Reads/writes detections CSVs. Drop-in compatible with the original column set."""

import csv
from pathlib import Path

from ..domain import Detection, VerifiedState
from . import paths

# Columns explicitly modeled on Detection. Anything else falls into Detection.extra.
_CORE_FIELDS = {
    "Campaign",
    "ARU",
    "Week",
    "Species",
    "Scientific_Name",
    "Confidence",
    "Start_Time",
    "End_Time",
    "Rank",
    "File",
    "Recording_Time",
    "Lat",
    "Lon",
    "Species_List",
    "Min_Conf",
    "Model",
    "Verified",
    "Corrected_Species",
    "Comment",
}

# Annotation columns added even if not present in the source CSV.
ANNOTATION_FIELDS = ("Verified", "Corrected_Species", "Comment")


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


def _row_to_detection(row: dict[str, str]) -> Detection:
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
        extra={k: v for k, v in row.items() if k not in _CORE_FIELDS},
    )


def _detection_to_row(d: Detection) -> dict[str, str]:
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


def _format_number(value: float) -> str:
    """Render integers without a trailing .0; floats stay as float reprs."""
    if value == int(value):
        return str(int(value))
    return repr(value)


def _read_csv(path: Path) -> tuple[list[Detection], list[str]]:
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        detections = []
        for row in reader:
            d = _row_to_detection(row)
            d.source_path = path
            detections.append(d)
    return detections, fieldnames


def _write_csv(path: Path, detections: list[Detection], fieldnames: list[str]) -> None:
    full_fields = list(fieldnames)
    for f in ANNOTATION_FIELDS:
        if f not in full_fields:
            full_fields.append(f)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=full_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(_detection_to_row(d) for d in detections)


class CsvDetectionRepository:
    """Reads and writes per-campaign detection CSVs.

    Each model run lands in its own file (<campaign>-detections-<model_key>.csv)
    so multiple runs can coexist for a campaign. Load enumerates every model
    file plus the legacy single-file fallback, concatenating in memory. Save
    routes each detection back to the file it was loaded from via
    Detection.source_path; brand-new detections (no source_path) fall back
    to the legacy file path. Per-file fieldnames are remembered so column
    order survives a load/save round trip.
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

    def save(self, output_base: Path, detections: list[Detection]) -> None:
        """Persist edits, grouped by campaign."""
        if not detections:
            return
        groups: dict[str, list[Detection]] = {}
        for d in detections:
            groups.setdefault(d.campaign, []).append(d)
        for campaign_name, rows in groups.items():
            if campaign_name:
                self.save_for_campaign(output_base, campaign_name, rows)

    def save_for_campaign(self, output_base: Path, campaign_name: str, detections: list[Detection]) -> None:
        """Write detections back, grouped by their source file.

        Edits load_for_campaign annotated each row with its source path, so
        a campaign with both birdnet and perch runs round-trips correctly:
        each detection lands in the same file it came from. Detections with
        no source_path (synthesized, not loaded) fall back to the legacy
        unsuffixed path.
        """
        legacy_fallback = paths.campaign_csv(output_base, campaign_name)
        groups: dict[Path, list[Detection]] = {}
        for d in detections:
            target = d.source_path or legacy_fallback
            groups.setdefault(target, []).append(d)
        for path, rows in groups.items():
            fieldnames = self._fieldnames_by_path.get(path) or list(_CORE_FIELDS)
            _write_csv(path, rows, fieldnames)
