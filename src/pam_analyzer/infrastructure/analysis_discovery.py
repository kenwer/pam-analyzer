"""Synthesize AnalysisRunResult from on-disk artifacts of a previous run.

Used at project load: if the project's output_base already contains detection
CSVs from earlier BirdNET runs, the BirdNET panel can show them without the
user re-running analysis. The synthesized result carries `from_disk=True` so
the UI can distinguish 'just finished' from 'previously produced'.

The disk layout we discover here is the inverse of what birdnet_runner.py
writes: see _write_summary_tables / _write_combined_csv / _write_week_tables.
"""

import re
from pathlib import Path

from ..domain import AnalysisRunResult, CampaignRunResult, WeekRunResult
from . import paths

_WEEK_FILENAME = re.compile(r"-week-(\d{2})-detections\.csv$")


def discover_analysis_result(output_base: Path, project_name: str) -> AnalysisRunResult | None:
    """Build an AnalysisRunResult from CSVs that already exist under output_base.

    Returns None when no campaign detection CSV is found (a clean project, or
    one where analysis has never been run). Missing companion files are not an
    error; they're recorded with their expected path so the panel can hide the
    buttons via Path.exists() checks.
    """
    if not output_base.exists():
        return None

    campaigns: list[CampaignRunResult] = []
    for sub in sorted(output_base.iterdir()):
        if not sub.is_dir():
            continue
        det_csv = paths.campaign_csv(output_base, sub.name)
        if not det_csv.exists():
            continue
        campaigns.append(_synthesize_campaign(output_base, sub.name))

    if not campaigns:
        return None

    return AnalysisRunResult(
        campaigns=tuple(campaigns),
        combined_csv=_optional(paths.combined_csv(output_base, project_name)),
        per_campaign_aru_csv=_optional(output_base / f"{project_name}-summary-per-campaign-aru.csv"),
        all_campaigns_csv=_optional(output_base / f"{project_name}-summary-all-campaigns-all-arus.csv"),
        elapsed=0.0,
        from_disk=True,
    )


def _synthesize_campaign(output_base: Path, campaign_name: str) -> CampaignRunResult:
    output_dir = output_base / campaign_name
    det_csv = paths.campaign_csv(output_base, campaign_name)
    return CampaignRunResult(
        campaign_name=campaign_name,
        output_dir=output_dir,
        detections_csv=det_csv,
        per_aru_csv=output_dir / f"{campaign_name}-summary-per-aru.csv",
        all_arus_csv=output_dir / f"{campaign_name}-summary-all-arus.csv",
        species_list_txt=_optional(output_dir / f"{campaign_name}-species-list.txt"),
        week_results=tuple(_discover_weeks(output_dir, campaign_name)),
        detection_count=_count_csv_rows(det_csv),
        wav_count=0,
        aru_count=0,
        elapsed=0.0,
    )


def _discover_weeks(output_dir: Path, campaign_name: str) -> list[WeekRunResult]:
    weeks: list[WeekRunResult] = []
    prefix = f"{campaign_name}-week-"
    for path in sorted(output_dir.glob(f"{prefix}*-detections.csv")):
        m = _WEEK_FILENAME.search(path.name)
        if m is None:
            continue
        week_num = int(m.group(1))
        weeks.append(
            WeekRunResult(
                week=week_num,
                detections_csv=path,
                per_aru_csv=output_dir / f"{prefix}{week_num:02d}-summary-per-aru.csv",
                all_arus_csv=output_dir / f"{prefix}{week_num:02d}-summary-all-arus.csv",
                species_list_txt=_optional(output_dir / f"{campaign_name}-species-list-week-{week_num:02d}.txt"),
            )
        )
    return weeks


def _optional(path: Path) -> Path | None:
    return path if path.exists() else None


def _count_csv_rows(path: Path) -> int:
    """Count data rows (excludes header). Streaming so it works on big CSVs."""
    try:
        with open(path, "rb") as f:
            total = sum(1 for _ in f)
    except OSError:
        return 0
    return max(0, total - 1)
