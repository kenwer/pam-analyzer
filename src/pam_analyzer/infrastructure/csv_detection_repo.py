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
        detections = [_row_to_detection(row) for row in reader]
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
    """Stores per-campaign and combined detection CSVs.

    Per-load original fieldnames are tracked so writes preserve column order.
    """

    def __init__(self) -> None:
        self._fieldnames_by_campaign: dict[str, list[str]] = {}
        self._fieldnames_combined: list[str] = []

    def load_for_campaign(self, output_base: Path, campaign_name: str) -> list[Detection]:
        path = paths.campaign_csv(output_base, campaign_name)
        if not path.exists():
            return []
        detections, fieldnames = _read_csv(path)
        self._fieldnames_by_campaign[campaign_name] = fieldnames
        return detections

    def load_combined(self, output_base: Path, project_name: str) -> list[Detection]:
        combined = paths.combined_csv(output_base, project_name)
        if combined.exists():
            detections, fieldnames = _read_csv(combined)
            self._fieldnames_combined = fieldnames
            return detections

        all_detections: list[Detection] = []
        if not output_base.exists():
            return all_detections
        for sub in sorted(output_base.iterdir()):
            if not sub.is_dir():
                continue
            csv_path = paths.campaign_csv(output_base, sub.name)
            if csv_path.exists():
                detections, fieldnames = _read_csv(csv_path)
                self._fieldnames_by_campaign[sub.name] = fieldnames
                if not self._fieldnames_combined:
                    self._fieldnames_combined = fieldnames
                all_detections.extend(detections)
        return all_detections

    def save(
        self,
        output_base: Path,
        detections: list[Detection],
        *,
        project_name: str | None = None,
        write_combined: bool = False,
    ) -> None:
        """Persist edits. Groups by campaign; optionally rewrites the combined CSV."""
        if not detections:
            return
        groups: dict[str, list[Detection]] = {}
        for d in detections:
            groups.setdefault(d.campaign, []).append(d)
        for campaign_name, rows in groups.items():
            if campaign_name:
                self.save_for_campaign(output_base, campaign_name, rows)
        if write_combined and project_name:
            self.save_combined(output_base, project_name, detections)

    def save_for_campaign(self, output_base: Path, campaign_name: str, detections: list[Detection]) -> None:
        fieldnames = self._fieldnames_by_campaign.get(campaign_name) or list(_CORE_FIELDS)
        _write_csv(paths.campaign_csv(output_base, campaign_name), detections, fieldnames)

    def save_combined(self, output_base: Path, project_name: str, detections: list[Detection]) -> None:
        fieldnames = self._fieldnames_combined or list(_CORE_FIELDS)
        _write_csv(paths.combined_csv(output_base, project_name), detections, fieldnames)
