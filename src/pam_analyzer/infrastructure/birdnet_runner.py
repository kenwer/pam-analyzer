"""BirdNET infrastructure adapter.

Wraps birdnet_analyzer in the AnalysisRunner port. All birdnet_analyzer
imports are confined to this module.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import birdnet_analyzer
import birdnet_analyzer.config as birdnet_cfg

from ..domain import (
    AnalysisProgress,
    AnalysisProgressSnapshot,
    AnalysisSettings,
    CampaignRunInput,
    CancelledError,
    FilterMode,
)
from ..domain.entities import AnalysisRunResult, CampaignRunResult, WeekRunResult
from . import paths


def _count_audio_files(campaign_dir: Path) -> int:
    return sum(
        1 for f in campaign_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in paths.AUDIO_EXTENSIONS
    )


def _week_from_path(path: Path) -> int | None:
    for part in path.parts:
        if part.startswith("week_"):
            try:
                return int(part.split("_", 1)[1])
            except (IndexError, ValueError):
                pass
    return None


def _parse_recording_time(stem: str) -> datetime | None:
    match = re.search(r"(\d{8}_\d{6})", stem)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
        except ValueError:
            pass
    return None


@lru_cache(maxsize=1)
def _locale_file_map() -> dict[str, Path]:
    labels_dir = Path(birdnet_analyzer.__file__).parent / "labels" / "V2.4"
    prefix = "BirdNET_GLOBAL_6K_V2.4_Labels_"
    return {
        p.stem[len(prefix):]: p
        for p in sorted(labels_dir.glob(f"{prefix}*.txt"))
    }


@lru_cache(maxsize=3)
def _load_locale_labels(locale: str) -> dict[str, str]:
    loc_path = _locale_file_map().get(locale)
    if not loc_path:
        return {}
    mapping: dict[str, str] = {}
    with open(loc_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "_" in line:
                sci, localized = line.split("_", 1)
                mapping[sci] = localized
    return mapping


def _get_available_locales() -> list[str]:
    return list(_locale_file_map())


def _prewarm_model() -> None:
    """Trigger model initialisation before threading to avoid extraction races."""
    try:
        from birdnet_analyzer.model import ensure_model_exists  # type: ignore[import]

        ensure_model_exists()
    except Exception:
        pass


def _parse_result_csv(
    result_csv: Path,
    campaign_name: str,
    campaign_dir: Path,
    audio_root: Path,
    week: int,
    settings: AnalysisSettings,
    locale_maps: dict[str, dict[str, str]],
    run_context: dict,
    preferred_lang_map: dict[str, str] | None = None,
) -> list[dict]:
    try:
        with open(result_csv, newline="", encoding="utf-8") as f:
            file_rows = list(csv.DictReader(f))
    except UnicodeDecodeError:
        with open(result_csv, newline="", encoding="latin-1") as f:
            file_rows = list(csv.DictReader(f))

    seg_groups: dict[tuple, list] = defaultdict(list)
    for row in file_rows:
        seg_groups[(row["Start (s)"], row["End (s)"])].append(row)
    seg_species_rank: dict[tuple, int] = {}
    for seg_rows in seg_groups.values():
        for rank, seg_row in enumerate(
            sorted(seg_rows, key=lambda r: float(r["Confidence"]), reverse=True),
            start=1,
        ):
            seg_species_rank[(seg_row["Start (s)"], seg_row["End (s)"], seg_row["Common name"])] = rank

    detections = []
    for row in file_rows:
        # Re-apply the threshold: stale *.BirdNET.results.csv files from an
        # earlier run with a lower min_conf may still linger in output_dir.
        if float(row["Confidence"]) < settings.min_conf:
            continue
        rank = seg_species_rank.get((row["Start (s)"], row["End (s)"], row["Common name"]), 0)
        scientific_name = row["Scientific name"]
        file_path = Path(row["File"])
        try:
            aru_number = file_path.relative_to(campaign_dir).parts[0]
        except ValueError:
            logging.warning("File %s is not under campaign dir %s; guessing ARU from path index", file_path, campaign_dir)
            aru_number = file_path.parts[-3] if len(file_path.parts) >= 3 else ""

        try:
            file_rel = file_path.relative_to(audio_root).as_posix()
        except ValueError:
            file_rel = file_path.as_posix()

        recording_time = _parse_recording_time(file_path.stem)
        locale_names = {
            f"Species_{loc}": (row["Common name"] if loc == "en" else locale_maps[loc].get(scientific_name, ""))
            for loc in settings.locales
        }
        species_name = (
            (preferred_lang_map.get(scientific_name) or row["Common name"])
            if preferred_lang_map
            else row["Common name"]
        )
        detections.append(
            {
                "Campaign": campaign_name,
                "ARU": aru_number,
                "Start_Time": row["Start (s)"],
                "End_Time": row["End (s)"],
                "Scientific_Name": scientific_name,
                "Species": species_name,
                **locale_names,
                "Confidence": row["Confidence"],
                "Rank": rank,
                "File": file_rel,
                "Recording_Time": str(recording_time) if recording_time else "",
                "Week": _week_from_path(result_csv) or week,
                **run_context,
                "Verified": "",
                "Corrected_Species": "",
                "Comment": "",
            }
        )
    return detections


def _write_summary_tables(
    detections: list[dict],
    output_dir: Path,
    locale_cols: list[str],
    campaign_name: str,
    file_prefix: str | None = None,
) -> tuple[Path, Path]:
    prefix = file_prefix or campaign_name

    per_aru: dict[tuple, dict] = defaultdict(
        lambda: {
            "count": 0,
            "max_conf": 0.0,
            "best_rank": float("inf"),
            "scientific_name": "",
            "locale_names": {},
        }
    )
    for row in detections:
        key = (row["ARU"], row["Species"])
        per_aru[key]["count"] += 1
        per_aru[key]["max_conf"] = max(per_aru[key]["max_conf"], float(row["Confidence"]))
        per_aru[key]["best_rank"] = min(per_aru[key]["best_rank"], int(row["Rank"]))
        per_aru[key]["scientific_name"] = row["Scientific_Name"]
        for col in locale_cols:
            per_aru[key]["locale_names"][col] = row.get(col, "")

    per_aru_path = output_dir / f"{prefix}-summary-per-aru.csv"
    with open(per_aru_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "Campaign",
                "ARU",
                "Scientific_Name",
                "Species",
                *locale_cols,
                "detection_count",
                "max_confidence",
                "best_species_rank",
            ],
        )
        writer.writeheader()
        for (aru, species_name), data in sorted(
            per_aru.items(), key=lambda x: (x[0][0], -x[1]["count"])
        ):
            writer.writerow(
                {
                    "Campaign": campaign_name,
                    "ARU": aru,
                    "Species": species_name,
                    "Scientific_Name": data["scientific_name"],
                    **data["locale_names"],
                    "detection_count": data["count"],
                    "max_confidence": f"{data['max_conf']:.4f}",
                    "best_species_rank": f"top-{int(data['best_rank'])}",
                }
            )

    global_agg: dict[str, dict] = defaultdict(
        lambda: {
            "count": 0,
            "max_conf": 0.0,
            "arus": set(),
            "scientific_name": "",
            "best_rank": float("inf"),
            "locale_names": {},
        }
    )
    for (aru, species_name), data in per_aru.items():
        global_agg[species_name]["count"] += data["count"]
        global_agg[species_name]["max_conf"] = max(global_agg[species_name]["max_conf"], data["max_conf"])
        global_agg[species_name]["arus"].add(aru)
        global_agg[species_name]["scientific_name"] = data["scientific_name"]
        global_agg[species_name]["best_rank"] = min(global_agg[species_name]["best_rank"], data["best_rank"])
        global_agg[species_name]["locale_names"].update(data["locale_names"])

    all_arus_path = output_dir / f"{prefix}-summary-all-arus.csv"
    with open(all_arus_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "Campaign",
                "Scientific_Name",
                "Species",
                *locale_cols,
                "detection_count",
                "aru_count",
                "max_confidence",
                "best_species_rank_any_aru",
            ],
        )
        writer.writeheader()
        for species_name, data in sorted(
            global_agg.items(), key=lambda x: -x[1]["count"]
        ):
            writer.writerow(
                {
                    "Campaign": campaign_name,
                    "Species": species_name,
                    "Scientific_Name": data["scientific_name"],
                    **data["locale_names"],
                    "detection_count": data["count"],
                    "aru_count": len(data["arus"]),
                    "max_confidence": f"{data['max_conf']:.4f}",
                    "best_species_rank_any_aru": f"top-{int(data['best_rank'])}",
                }
            )

    return per_aru_path, all_arus_path


def _write_week_tables(
    detections: list[dict],
    output_dir: Path,
    fieldnames: list[str],
    locale_cols: list[str],
    campaign_name: str,
) -> list[WeekRunResult]:
    by_week: dict[int, list[dict]] = defaultdict(list)
    for d in detections:
        w = d.get("Week")
        if w not in (None, -1):
            by_week[int(w)].append(d)

    results = []
    for week_num in sorted(by_week):
        week_dets = by_week[week_num]
        prefix = f"{campaign_name}-week-{week_num:02d}"

        det_csv = output_dir / f"{prefix}-detections.csv"
        with open(det_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(week_dets)

        per_aru_csv, all_arus_csv = _write_summary_tables(
            week_dets, output_dir, locale_cols, campaign_name, file_prefix=prefix
        )
        species_txt = output_dir / f"{campaign_name}-species-list-week-{week_num:02d}.txt"
        results.append(
            WeekRunResult(
                week=week_num,
                detections_csv=det_csv,
                per_aru_csv=per_aru_csv,
                all_arus_csv=all_arus_csv,
                species_list_txt=species_txt if species_txt.exists() else None,
            )
        )
    return results


def _write_combined_csv(
    named_results: list[tuple[str, CampaignRunResult]],
    output_dir: Path,
    project_name: str,
) -> Path:
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
        global_agg[species_name]["max_conf"] = max(global_agg[species_name]["max_conf"], float(row["max_confidence"]))
        global_agg[species_name]["campaigns"].add(row["Campaign"])
        global_agg[species_name]["arus"].add(row["ARU"])
        global_agg[species_name]["scientific_name"] = row["Scientific_Name"]
        rank_str = row.get("best_species_rank", "top-99").replace("top-", "")
        try:
            global_agg[species_name]["best_rank"] = min(global_agg[species_name]["best_rank"], int(rank_str))
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


def _emit(
    progress: AnalysisProgress,
    *,
    campaign: str,
    campaign_index: int,
    total_campaigns: int,
    files_done: int,
    files_total: int,
    phase: str,
) -> None:
    progress.report(
        AnalysisProgressSnapshot(
            campaign=campaign,
            campaign_index=campaign_index,
            total_campaigns=total_campaigns,
            files_done=files_done,
            files_total=files_total,
            phase=phase,
        )
    )


def _run_one(
    ci: CampaignRunInput,
    output_dir: Path,
    settings: AnalysisSettings,
    preferred_lang: str,
    audio_root: Path,
    progress: AnalysisProgress,
    campaign_index: int,
    total_campaigns: int,
) -> CampaignRunResult:
    campaign_name = ci.name
    t0 = time.monotonic()
    output_dir.mkdir(parents=True, exist_ok=True)

    _emit(
        progress,
        campaign=campaign_name,
        campaign_index=campaign_index,
        total_campaigns=total_campaigns,
        files_done=0,
        files_total=0,
        phase="preparing",
    )

    week = -1
    slist: str | None = None
    if ci.mode == FilterMode.LOCATION and ci.location is not None:
        lat: float | None = ci.location.latitude
        lon: float | None = ci.location.longitude
        week_dirs = sorted(d for d in ci.folder.rglob("week_*") if d.is_dir())
    else:
        lat, lon = None, None
        week_dirs = []
        if ci.species_list_text is not None:
            # Write the species list to a file that birdnet_analyzer can read
            slist_path = output_dir / f"{campaign_name}-species-list-input.txt"
            slist_path.write_text(ci.species_list_text, encoding="utf-8")
            slist = str(slist_path)

    wav_count = _count_audio_files(ci.folder)
    num_threads = min(os.cpu_count() or 4, 8)
    analyze_kwargs = {
        "min_conf": settings.min_conf,
        "slist": slist,
        "lat": lat if lat is not None else -1,
        "lon": lon if lon is not None else -1,
        "overlap": settings.overlap,
        "top_n": None,
        "rtype": "csv",
        "combine_results": False,
        "threads": num_threads,
        "locale": "en",  # birdnet_analyzer outputs English labels; non-English locales are mapped post-hoc
    }

    if week_dirs:
        for week_dir in week_dirs:
            if progress.is_cancelled():
                raise CancelledError()
            dir_week = _week_from_path(week_dir)
            if dir_week is None:
                continue
            _emit(
                progress,
                campaign=campaign_name,
                campaign_index=campaign_index,
                total_campaigns=total_campaigns,
                files_done=0,
                files_total=wav_count,
                phase="analyzing",
            )
            week_out = output_dir / week_dir.relative_to(ci.folder)
            week_out.mkdir(parents=True, exist_ok=True)
            birdnet_analyzer.analyze(str(week_dir), output=str(week_out), week=dir_week, **analyze_kwargs)
            _emit(
                progress,
                campaign=campaign_name,
                campaign_index=campaign_index,
                total_campaigns=total_campaigns,
                files_done=wav_count,
                files_total=wav_count,
                phase="parsing",
            )
    else:
        _emit(
            progress,
            campaign=campaign_name,
            campaign_index=campaign_index,
            total_campaigns=total_campaigns,
            files_done=0,
            files_total=wav_count,
            phase="analyzing",
        )
        birdnet_analyzer.analyze(str(ci.folder), output=str(output_dir), week=week, **analyze_kwargs)
        _emit(
            progress,
            campaign=campaign_name,
            campaign_index=campaign_index,
            total_campaigns=total_campaigns,
            files_done=wav_count,
            files_total=wav_count,
            phase="parsing",
        )

    result_csvs = sorted(output_dir.rglob("*.BirdNET.results.csv"))

    detections_csv = output_dir / f"{campaign_name}-detections.csv"
    per_aru_csv_path = output_dir / f"{campaign_name}-summary-per-aru.csv"
    all_arus_csv_path = output_dir / f"{campaign_name}-summary-all-arus.csv"

    if not result_csvs:
        return CampaignRunResult(
            campaign_name=campaign_name,
            output_dir=output_dir,
            detections_csv=detections_csv,
            per_aru_csv=per_aru_csv_path,
            all_arus_csv=all_arus_csv_path,
            species_list_txt=None,
            week_results=(),
            detection_count=0,
            wav_count=wav_count,
            aru_count=0,
            elapsed=time.monotonic() - t0,
        )

    run_context = {
        "Lat": lat if lat is not None else "",
        "Lon": lon if lon is not None else "",
        "Species_List": slist or "",
        "Min_Conf": settings.min_conf,
        "Model": Path(birdnet_cfg.MODEL_PATH).name if birdnet_cfg.MODEL_PATH else "",
    }

    preferred_lang_map = _load_locale_labels(preferred_lang)
    locale_maps = {
        loc: _load_locale_labels(loc)
        for loc in settings.locales
        if loc != "en"
    }
    locale_cols = [f"Species_{loc}" for loc in settings.locales]

    fieldnames = [
        "Campaign",
        "ARU",
        "Start_Time",
        "End_Time",
        "Scientific_Name",
        "Species",
        *locale_cols,
        "Confidence",
        "Rank",
        "File",
        "Recording_Time",
        "Week",
        *run_context.keys(),
        "Verified",
        "Corrected_Species",
        "Comment",
    ]

    detections: list[dict] = []
    with open(detections_csv, "w", newline="", encoding="utf-8") as outfile:
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()
        for result_csv in result_csvs:
            if progress.is_cancelled():
                raise CancelledError()
            rows = _parse_result_csv(
                result_csv,
                campaign_name,
                ci.folder,
                audio_root,
                week,
                settings,
                locale_maps,
                run_context,
                preferred_lang_map,
            )
            writer.writerows(rows)
            detections.extend(rows)

    if progress.is_cancelled():
        raise CancelledError()

    _emit(
        progress,
        campaign=campaign_name,
        campaign_index=campaign_index,
        total_campaigns=total_campaigns,
        files_done=wav_count,
        files_total=wav_count,
        phase="summarizing",
    )

    per_aru_csv_path, all_arus_csv_path = _write_summary_tables(detections, output_dir, locale_cols, campaign_name)

    # Export geographic species list(s) in location mode. Done before
    # _write_week_tables so each WeekRunResult can record whether its
    # species-list file actually exists.
    warnings: list[str] = []
    species_list_txt: Path | None = None
    if lat is not None and lon is not None:
        try:
            from birdnet_analyzer.species.utils import get_species_list  # type: ignore[import]

            if week_dirs:
                unique_weeks = sorted({w for d in week_dirs if (w := _week_from_path(d)) is not None})
                for w in unique_weeks:
                    geo_species = get_species_list(lat, lon, w, threshold=0.03)
                    (output_dir / f"{campaign_name}-species-list-week-{w:02d}.txt").write_text(
                        "\n".join(geo_species) + "\n", encoding="utf-8"
                    )
            else:
                geo_species = get_species_list(lat, lon, week, threshold=0.03)
                sl_path = output_dir / f"{campaign_name}-species-list.txt"
                sl_path.write_text("\n".join(geo_species) + "\n", encoding="utf-8")
                species_list_txt = sl_path
        except Exception as exc:
            msg = f"Failed to export geographic species list: {exc}"
            print(f"[birdnet] {msg}", file=sys.stderr)
            warnings.append(msg)

    week_results = _write_week_tables(detections, output_dir, fieldnames, locale_cols, campaign_name)

    _emit(
        progress,
        campaign=campaign_name,
        campaign_index=campaign_index,
        total_campaigns=total_campaigns,
        files_done=wav_count,
        files_total=wav_count,
        phase="done",
    )

    return CampaignRunResult(
        campaign_name=campaign_name,
        output_dir=output_dir,
        detections_csv=detections_csv,
        per_aru_csv=per_aru_csv_path,
        all_arus_csv=all_arus_csv_path,
        species_list_txt=species_list_txt,
        week_results=tuple(week_results),
        detection_count=len(detections),
        wav_count=wav_count,
        aru_count=len({row["ARU"] for row in detections}),
        elapsed=time.monotonic() - t0,
        warnings=tuple(warnings),
    )


class BirdnetAnalyzerRunner:
    """AnalysisRunner port implementation backed by birdnet_analyzer."""

    def count_audio_files(self, campaign_dir: Path) -> int:
        return _count_audio_files(campaign_dir)

    def available_locales(self) -> list[str]:
        return _get_available_locales()

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
        _prewarm_model()
        output_base.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()

        results: list[CampaignRunResult] = []
        total = len(campaigns)
        for i, ci in enumerate(campaigns, start=1):
            if progress.is_cancelled():
                raise CancelledError()
            camp_out = output_base / ci.name
            result = _run_one(
                ci,
                camp_out,
                settings,
                preferred_lang,
                audio_root,
                progress,
                i,
                total,
            )
            results.append(result)

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
