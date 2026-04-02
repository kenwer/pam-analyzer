"""Data management for the Examine panel.

Handles detection CSV loading/saving, campaign discovery, cell editing,
and per-ARU/species filtering for the Examine panel.
"""

from pathlib import Path

from pam_analyzer.core.birdnet_runner import get_species_options
from pam_analyzer.core.campaign_settings import discover_campaigns
from pam_analyzer.core.detections_io import ANNOTATION_FIELDS, read_csv, write_csv

ALL_CAMPAIGNS = 'all'


class ExamineDataManager:
    """Manages detection data state and persistence for the Examine panel."""

    def __init__(self) -> None:
        self.detections: list[dict] = []
        self.fieldnames: list[str] = []
        self.species_options: list[str] = []
        self.campaign_paths: dict[str, Path] = {}
        self.current_campaign: str = ALL_CAMPAIGNS

    def discover(self, audio_root: Path) -> dict[str, Path]:
        """Re-discover campaigns under audio_root."""
        self.campaign_paths = discover_campaigns(audio_root)
        return self.campaign_paths

    def load_campaign(
        self,
        name: str,
        output_base: Path,
        preferred_lang: str,
    ) -> tuple[list[dict], list[str]] | None:
        """Load detections for a single campaign.

        Returns (rows, fieldnames) or None if the CSV does not exist.
        Also updates ``species_options`` from the campaign output.
        """
        self.current_campaign = name
        output_dir = output_base / name
        self.species_options = get_species_options(output_dir, name, preferred_lang)
        csv_path = output_dir / f'{name}-detections.csv'
        if not csv_path.exists():
            return None
        return read_csv(csv_path)

    def load_all_campaigns(
        self,
        output_base: Path,
        project_name: str,
        preferred_lang: str,
    ) -> tuple[list[dict], list[str]] | None:
        """Load the project-level combined CSV, or concatenate per-campaign CSVs.

        Returns (rows, fieldnames) or None if no detections exist.
        Also updates ``species_options`` from all campaigns.
        """
        self.current_campaign = ALL_CAMPAIGNS

        # Gather species options from all campaigns
        all_species: set[str] = set()
        for name in self.campaign_paths:
            all_species.update(get_species_options(output_base / name, name, preferred_lang))
        self.species_options = sorted(all_species)

        # Prefer the combined project-level CSV
        combined_csv = output_base / f'{project_name}-detections.csv'
        if combined_csv.exists():
            return read_csv(combined_csv)

        # Fall back: concatenate individual campaign CSVs
        all_rows: list[dict] = []
        fieldnames: list[str] = []
        for name in self.campaign_paths:
            campaign_csv = output_base / name / f'{name}-detections.csv'
            if campaign_csv.exists():
                rows, fnames = read_csv(campaign_csv)
                if not fieldnames:
                    fieldnames = fnames
                all_rows.extend(rows)

        return (all_rows, fieldnames) if all_rows else None

    def set_detections(
        self,
        rows: list[dict],
        fieldnames: list[str],
        max_rows: int = 0,
    ) -> int:
        """Store loaded detections, optionally truncating.

        Returns the number of rows that were truncated (0 if none).
        """
        truncated = max(0, len(rows) - max_rows) if max_rows > 0 else 0
        self.detections = rows[:max_rows] if max_rows > 0 else rows
        self.fieldnames = fieldnames
        return truncated

    def update_cell(
        self,
        file_val: str,
        start_val: object,
        sci_val: str,
        annotation_data: dict,
    ) -> None:
        """Update annotation fields for the matching detection row."""
        for row in self.detections:
            if row.get('File') == file_val and row.get('Start_Time') == start_val and row.get('Scientific_Name') == sci_val:
                for field in ANNOTATION_FIELDS:
                    if field in annotation_data:
                        row[field] = annotation_data[field]
                break

    def save_detections(self, output_base: Path, project_name: str | None = None) -> None:
        """Rewrite detections CSV(s) with current in-memory rows."""
        if not self.detections or not self.fieldnames:
            return

        # Ensure annotation columns are in fieldnames
        fieldnames = list(self.fieldnames)
        for f in ANNOTATION_FIELDS:
            if f not in fieldnames:
                fieldnames.append(f)

        # Group rows by campaign and rewrite each campaign's CSV
        campaign_rows: dict[str, list[dict]] = {}
        for row in self.detections:
            name = row.get('Campaign', '')
            campaign_rows.setdefault(name, []).append(row)

        for campaign_name, rows in campaign_rows.items():
            if not campaign_name:
                continue
            csv_path = output_base / campaign_name / f'{campaign_name}-detections.csv'
            if csv_path.parent.exists():
                write_csv(csv_path, rows, fieldnames)

        # Also rewrite project-level detections CSV when viewing all campaigns
        if self.current_campaign == ALL_CAMPAIGNS and project_name:
            combined_csv = output_base / f'{project_name}-detections.csv'
            if combined_csv.exists():
                write_csv(combined_csv, self.detections, fieldnames)

    def clear(self) -> None:
        """Reset detection state."""
        self.detections = []
        self.fieldnames = []

    def filter_max_per_aru_species(self, max_per: int) -> list[dict]:
        """Return only the top-X rows per (ARU, Species) pair, ranked by Confidence desc."""
        if max_per < 1:  # 0 means "All", no filtering
            return self.detections
        sorted_rows = sorted(
            self.detections,
            key=lambda r: (
                str(r.get('ARU', '')),
                str(r.get('Species', '')),
                -(r.get('Confidence') or 0.0),
            ),
        )
        result: list[dict] = []
        counts: dict[tuple, int] = {}
        for row in sorted_rows:
            key = (str(row.get('ARU', '')), str(row.get('Species', '')))
            n = counts.get(key, 0)
            if n < max_per:
                result.append(row)
                counts[key] = n + 1
        return result
