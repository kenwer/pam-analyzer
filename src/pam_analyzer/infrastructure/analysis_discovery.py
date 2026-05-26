"""Synthesize AnalysisRunResult from on-disk artifacts of a previous run.

Used at project load and after every successful run: the panel asks for
a fresh on-disk snapshot, so anything the user has accumulated under
output_base/<campaign>/<campaign>-detections-<model>.csv shows up.
"""

from pathlib import Path

from ..domain import AnalysisRunResult, CampaignRunResult
from . import paths


def discover_analysis_result(output_base: Path) -> AnalysisRunResult | None:
    """Build an AnalysisRunResult from campaign CSVs that exist under output_base.

    Returns None when no campaign detection CSV is found (a clean project, or
    one where analysis has never been run). A missing species-list file is
    not an error; it is recorded as None.

    One CampaignRunResult is emitted per CSV so multiple model runs of the
    same campaign coexist as sibling rows tagged with model_key. The panel
    filters by the active model_key to show just the matching run.
    """
    if not output_base.exists():
        return None

    campaigns: list[CampaignRunResult] = []
    for sub in sorted(output_base.iterdir()):
        if not sub.is_dir():
            continue
        for csv_path in paths.campaign_csvs(output_base, sub.name):
            campaigns.append(_synthesize_campaign(output_base, sub.name, csv_path))

    if not campaigns:
        return None

    return AnalysisRunResult(campaigns=tuple(campaigns), elapsed=0.0)


def _synthesize_campaign(
    output_base: Path, campaign_name: str, csv_path: Path
) -> CampaignRunResult:
    """Build a CampaignRunResult for one on-disk detection CSV.

    model_key is inferred from the filename suffix: <campaign>-detections-<key>.csv.
    """
    output_dir = output_base / campaign_name
    prefix = f"{campaign_name}-detections-"
    return CampaignRunResult(
        campaign_name=campaign_name,
        output_dir=output_dir,
        detections_csv=csv_path,
        species_list_txt=_optional(output_dir / f"{campaign_name}-species-list.txt"),
        detection_count=_count_csv_rows(csv_path),
        wav_count=0,
        aru_count=0,
        elapsed=0.0,
        model_key=csv_path.stem.removeprefix(prefix),
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
