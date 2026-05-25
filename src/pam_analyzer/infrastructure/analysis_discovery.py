"""Synthesize AnalysisRunResult from on-disk artifacts of a previous run.

Used at project load: if the project's output_base already contains
<campaign>-detections.csv files from earlier BirdNET runs, the BirdNET panel
can show them without the user re-running analysis. The synthesized result
carries `from_disk=True` so the UI can distinguish 'just finished' from
'previously produced'.
"""

from pathlib import Path

from ..domain import AnalysisRunResult, CampaignRunResult
from . import paths


def discover_analysis_result(output_base: Path) -> AnalysisRunResult | None:
    """Build an AnalysisRunResult from campaign CSVs that exist under output_base.

    Returns None when no campaign detection CSV is found (a clean project, or
    one where analysis has never been run). A missing species-list file is
    not an error; it is recorded as None.
    """
    if not output_base.exists():
        return None

    campaigns: list[CampaignRunResult] = []
    for sub in sorted(output_base.iterdir()):
        if not sub.is_dir():
            continue
        csv_paths = paths.campaign_csvs(output_base, sub.name)
        if not csv_paths:
            continue
        campaigns.append(_synthesize_campaign(output_base, sub.name, csv_paths))

    if not campaigns:
        return None

    return AnalysisRunResult(campaigns=tuple(campaigns), elapsed=0.0, from_disk=True)


def _synthesize_campaign(
    output_base: Path, campaign_name: str, csv_paths: list[Path]
) -> CampaignRunResult:
    """Build a CampaignRunResult that covers every model CSV in the campaign.

    detections_csv is a single Path field by contract, so we pick the most
    recently modified one as the canonical "click to open" target; the
    detection_count sums across every file so the results panel reflects
    aggregated reality. csv_paths is the full list (legacy + per-model).
    """
    output_dir = output_base / campaign_name
    primary = max(csv_paths, key=lambda p: p.stat().st_mtime)
    total_rows = sum(_count_csv_rows(p) for p in csv_paths)
    return CampaignRunResult(
        campaign_name=campaign_name,
        output_dir=output_dir,
        detections_csv=primary,
        species_list_txt=_optional(output_dir / f"{campaign_name}-species-list.txt"),
        detection_count=total_rows,
        wav_count=0,
        aru_count=0,
        elapsed=0.0,
    )


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
