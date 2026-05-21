"""BirdNET multi-campaign coordinator.

Implements the AnalysisRunner protocol by wrapping BirdnetRunner (per-campaign
subprocess adapter), normalising progress reporting across campaign boundaries,
and writing cross-campaign rollup CSVs when more than one campaign is analysed.
"""

from __future__ import annotations

import csv
import time
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from ..domain import (
    AnalysisProgress,
    AnalysisProgressSnapshot,
    AnalysisSettings,
    CampaignRunInput,
    CancelledError,
)
from ..domain.entities import AnalysisRunResult, CampaignRunResult
from .birdnet_runner import BirdnetRunner


class _RunGlobalProgress:
    """Translate per-campaign snapshots from _run_one to run-global counts.

    _run_one reports files_done/files_total scoped to one campaign so it
    can be reasoned about in isolation. When the user runs more than one
    campaign at once, that would make the UI bar fill to 100% and snap
    back to 0% at every campaign boundary. This adapter sits between
    BirdnetRunner.run_campaign and the real AnalysisProgress port and
    rewrites each snapshot's files_done/files_total to refer to the entire
    run, while leaving phase, campaign, and phase_detail untouched so the
    label still tells the user which campaign is active.
    """

    def __init__(self, inner: AnalysisProgress, run_total: int) -> None:
        self._inner = inner
        self._run_total = run_total
        self._baseline = 0

    def start_campaign(self, files_done_so_far: int) -> None:
        """Set the offset added to every subsequent snapshot's files_done."""
        self._baseline = files_done_so_far

    def report(self, snapshot: AnalysisProgressSnapshot) -> None:
        global_done = min(self._baseline + snapshot.files_done, self._run_total)
        self._inner.report(
            replace(snapshot, files_done=global_done, files_total=self._run_total)
        )

    def is_cancelled(self) -> bool:
        return self._inner.is_cancelled()


def _write_combined_csv(
    named_results: list[tuple[str, CampaignRunResult]],
    output_dir: Path,
    project_name: str,
) -> Path:
    """Concatenate every campaign's detections CSV into one project file."""
    fieldnames: list[str] | None = None
    for _, result in named_results:
        if result.detections_csv.exists():
            with open(result.detections_csv, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames:
                    fieldnames = list(reader.fieldnames)
                    break

    combined_path = output_dir / f"{project_name}-detections.csv"
    if not fieldnames:
        return combined_path

    with open(combined_path, "w", newline="", encoding="utf-8") as outfile:
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()
        for _, result in named_results:
            if not result.detections_csv.exists():
                continue
            with open(result.detections_csv, newline="", encoding="utf-8") as infile:
                for row in csv.DictReader(infile):
                    writer.writerow(row)

    return combined_path


def _write_project_summaries(
    named_results: list[tuple[str, CampaignRunResult]],
    output_dir: Path,
    project_name: str,
    locale_cols: list[str],
) -> tuple[Path, Path]:
    """Build cross-campaign aggregates from each campaign's per-ARU summary."""
    all_rows: list[dict] = []
    fieldnames: list[str] | None = None
    for _, result in named_results:
        if not result.per_aru_csv.exists():
            continue
        with open(result.per_aru_csv, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if fieldnames is None and reader.fieldnames:
                fieldnames = list(reader.fieldnames)
            all_rows.extend(reader)

    per_campaign_aru_path = output_dir / f"{project_name}-summary-per-campaign-aru.csv"
    if fieldnames:
        with open(per_campaign_aru_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_rows)

    global_agg: dict[str, dict] = defaultdict(
        lambda: {
            "count": 0,
            "max_conf": 0.0,
            "campaigns": set(),
            "arus": set(),
            "scientific_name": "",
            "best_rank": float("inf"),
            "locale_names": {},
        }
    )
    for row in all_rows:
        species_name = row["Species"]
        global_agg[species_name]["count"] += int(row["detection_count"])
        global_agg[species_name]["max_conf"] = max(
            global_agg[species_name]["max_conf"], float(row["max_confidence"])
        )
        global_agg[species_name]["campaigns"].add(row["Campaign"])
        global_agg[species_name]["arus"].add(row["ARU"])
        global_agg[species_name]["scientific_name"] = row["Scientific_Name"]
        rank_str = row.get("best_species_rank", "top-99").replace("top-", "")
        try:
            global_agg[species_name]["best_rank"] = min(
                global_agg[species_name]["best_rank"], int(rank_str)
            )
        except ValueError:
            pass
        for col in locale_cols:
            if row.get(col):
                global_agg[species_name]["locale_names"][col] = row[col]

    all_campaigns_path = output_dir / f"{project_name}-summary-all-campaigns-all-arus.csv"
    global_fieldnames = [
        "Scientific_Name",
        "Species",
        *locale_cols,
        "detection_count",
        "campaign_count",
        "aru_count",
        "max_confidence",
        "best_species_rank_any_campaign",
    ]
    with open(all_campaigns_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=global_fieldnames)
        writer.writeheader()
        for species_name, data in sorted(global_agg.items(), key=lambda x: -x[1]["count"]):
            writer.writerow(
                {
                    "Species": species_name,
                    "Scientific_Name": data["scientific_name"],
                    **data["locale_names"],
                    "detection_count": data["count"],
                    "campaign_count": len(data["campaigns"]),
                    "aru_count": len(data["arus"]),
                    "max_confidence": f"{data['max_conf']:.4f}",
                    "best_species_rank_any_campaign": f"top-{int(data['best_rank'])}",
                }
            )

    return per_campaign_aru_path, all_campaigns_path


class BirdnetAnalyzer:
    """AnalysisRunner implementation that coordinates multi-campaign BirdNET runs.

    Wraps BirdnetRunner (per-campaign subprocess adapter), normalises
    progress across campaign boundaries via _RunGlobalProgress, and writes
    cross-campaign rollup CSVs when more than one campaign is analysed.
    """

    def __init__(self, runner: BirdnetRunner) -> None:
        self._runner = runner

    def count_audio_files(self, campaign_dir: Path) -> int:
        return self._runner.count_audio_files(campaign_dir)

    def available_locales(self) -> list[str]:
        return self._runner.available_locales()

    def run(
        self,
        *,
        campaigns: list[CampaignRunInput],
        output_base: Path,
        project_name: str,
        settings: AnalysisSettings,
        preferred_lang: str,
        audio_root: Path,
        progress: AnalysisProgress,
    ) -> AnalysisRunResult:
        self._runner.prewarm()
        output_base.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()

        per_campaign_totals = [self._runner.count_audio_files(ci.folder) for ci in campaigns]
        run_total = sum(per_campaign_totals)
        run_progress = _RunGlobalProgress(progress, run_total)

        results: list[CampaignRunResult] = []
        total = len(campaigns)
        files_completed = 0
        for i, (ci, ci_total) in enumerate(zip(campaigns, per_campaign_totals, strict=True), start=1):
            if progress.is_cancelled():
                raise CancelledError()
            run_progress.start_campaign(files_completed)
            camp_out = output_base / ci.name
            result = self._runner.run_campaign(
                ci,
                camp_out,
                settings,
                preferred_lang,
                audio_root,
                run_progress,
                i,
                total,
            )
            results.append(result)
            files_completed += ci_total

        combined_csv = per_campaign_aru_csv = all_campaigns_csv = None
        if len(results) > 1:
            locale_cols = [f"Species_{loc}" for loc in settings.locales]
            named = [(r.campaign_name, r) for r in results]
            combined_csv = _write_combined_csv(named, output_base, project_name)
            per_campaign_aru_csv, all_campaigns_csv = _write_project_summaries(
                named,
                output_base,
                project_name,
                locale_cols,
            )

        return AnalysisRunResult(
            campaigns=tuple(results),
            combined_csv=combined_csv,
            per_campaign_aru_csv=per_campaign_aru_csv,
            all_campaigns_csv=all_campaigns_csv,
            elapsed=time.monotonic() - t0,
        )
