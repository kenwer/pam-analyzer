"""BirdNET infrastructure adapter.

Wraps birdnet_analyzer in a per-campaign subprocess loop. All
birdnet_analyzer imports are confined to this module so the rest of the
codebase stays agnostic of the underlying analyzer.

BirdnetRunner is the AnalysisRunner implementation: its run() iterates the
campaigns, normalises progress across campaign boundaries via
_RunGlobalProgress, and delegates each campaign to _run_campaign.

Flow when BirdnetRunner.run is called:

    BirdnetRunner.run(...)                    # public entry point
      -> _run_campaign(...)                   # once per campaign
           -> _emit_progress(phase="preparing")
           -> _emit_progress(phase="analyzing")     # bar appears at 0/N
           -> _analyze_with_per_file_progress() # one call per week_dir
                                          # (location mode) or one call
                                          # for the whole campaign
                                          # (list / global)
                -> Pool.imap_unordered(
                       _analyze_file_with_path, flist)
                     -> _emit_progress("analyzing", k/N) # per finished file
           -> _emit_progress(phase="parsing")
           -> _parse_result_csv() per
              *.BirdNET.results.csv        # one file per audio input
           -> writes <campaign>-detections.csv  # the only CSV this code emits
           -> _emit_progress(phase="done")

Cancellation: each week-dir boundary, each per-file Pool yield, and each
result-CSV parse checks progress.is_cancelled(); the Pool loop also calls
pool.terminate() before raising CancelledError so in-flight workers stop
immediately instead of draining.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import replace
from datetime import datetime
from functools import lru_cache
from multiprocessing import Pool
from pathlib import Path

import birdnet_analyzer
import birdnet_analyzer.config as birdnet_cfg
from birdnet_analyzer.analyze.core import _set_params
from birdnet_analyzer.analyze.utils import analyze_file as _bn_analyze_file
from birdnet_analyzer.analyze.utils import save_analysis_params

from ..domain import (
    AnalysisProgress,
    AnalysisProgressSnapshot,
    AnalysisRunResult,
    AnalysisSettings,
    CampaignRunInput,
    CancelledError,
    FilterMode,
)
from ..domain.entities import CampaignRunResult
from . import paths


def _count_audio_files(campaign_dir: Path) -> int:
    """Count audio files under a campaign folder.

    Called once at the start of each _run_campaign to populate files_total on
    progress snapshots, and also exposed via the public port for the UI's
    pre-run sanity check.
    """
    return sum(
        1 for f in campaign_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in paths.AUDIO_EXTENSIONS
    )


def _week_from_path(path: Path) -> int | None:
    """Extract the ISO week number from a 'week_NN' path segment, or None.

    Location-mode campaigns are organised as <campaign>/<aru>/week_NN/...
    so we can run birdnet_analyzer per week with the correct seasonal
    species filter. Returns None when no such segment is present.
    """
    for part in path.parts:
        if part.startswith("week_"):
            try:
                return int(part.split("_", 1)[1])
            except (IndexError, ValueError):
                pass
    return None


def _parse_recording_time(stem: str) -> datetime | None:
    """Pull a 'YYYYMMDD_HHMMSS' timestamp out of an audio filename.

    ARU filenames produced in the field embed the start time; we surface
    it on every detection row so downstream analysis has a real datetime
    rather than just a file path.
    """
    match = re.search(r"(\d{8}_\d{6})", stem)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
        except ValueError:
            pass
    return None


@lru_cache(maxsize=1)
def _locale_file_map() -> dict[str, Path]:
    """Map locale code (e.g. 'de', 'fr') to its BirdNET labels file path.

    The list of label files shipped with the model is constant per
    process, so the directory listing is cached after the first call.
    Consumed by _load_locale_labels and exposed to the UI via
    _get_available_locales.
    """
    labels_dir = Path(birdnet_analyzer.__file__).parent / "labels" / "V2.4"
    prefix = "BirdNET_GLOBAL_6K_V2.4_Labels_"
    return {
        p.stem[len(prefix):]: p
        for p in sorted(labels_dir.glob(f"{prefix}*.txt"))
    }


@lru_cache(maxsize=3)
def _load_locale_labels(locale: str) -> dict[str, str]:
    """Load a {scientific_name: localized_common_name} mapping for a locale.

    Cached because every detection row in a campaign looks up the same
    handful of locales; reading the labels file once per locale per
    process is enough. Returns {} for unknown locales rather than raising.
    """
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
    """Return locale codes the language picker in the UI should offer."""
    return list(_locale_file_map())


def _prewarm_model() -> None:
    """Force model extraction on the main thread before the worker Pool starts.

    Called once at the top of BirdnetRunner.run. The TFLite model
    file is unpacked from the wheel on first use; if several Pool workers
    race that unpack we get sporadic 'file not found' or truncated-file
    errors. Touching the model once up front sidesteps the race.
    """
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
    """Turn one BirdNET per-file results CSV into normalised detection rows.

    Called during the 'parsing' phase of _run_campaign, once for every
    '*.BirdNET.results.csv' file that birdnet_analyzer wrote (one per
    input audio file). The rows it returns are streamed straight into
    the combined per-campaign detections CSV.

    The function also derives the ARU id from the audio path, computes a
    per-segment species rank (most confident species at that timestamp =
    rank 1), and joins in localised common names from locale_maps.
    """
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


class _RunGlobalProgress:
    """Translate per-campaign snapshots from _run_campaign to run-global counts.

    _run_campaign reports files_done/files_total scoped to one campaign so it
    can be reasoned about in isolation. When the user runs more than one
    campaign at once, that would make the UI bar fill to 100% and snap back
    to 0% at every campaign boundary. This adapter sits between _run_campaign
    and the real AnalysisProgress port and rewrites each snapshot's
    files_done/files_total to refer to the entire run, while leaving phase,
    campaign, and phase_detail untouched so the label still tells the user
    which campaign is active.
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


def _emit_progress(
    progress: AnalysisProgress,
    *,
    campaign: str,
    campaign_index: int,
    total_campaigns: int,
    files_done: int,
    files_total: int,
    phase: str,
    phase_detail: str | None = None,
) -> None:
    """Build a snapshot and forward it to the AnalysisProgress port.

    Single funnel for every progress report so we can't accidentally
    omit a field. AnalysisWorker translates the report into a Qt signal
    that the BirdNetPanel renders on the UI thread.
    """
    progress.report(
        AnalysisProgressSnapshot(
            campaign=campaign,
            campaign_index=campaign_index,
            total_campaigns=total_campaigns,
            files_done=files_done,
            files_total=files_total,
            phase=phase,
            phase_detail=phase_detail,
        )
    )


def _analyze_file_with_path(item):
    """Pool worker shim: run birdnet analyze_file and return the input path.

    birdnet_analyzer's analyze_file returns a dict of output paths, not
    the input. We need the input filename to update the progress label,
    so this wrapper hands it back. Top-level so multiprocessing can
    pickle it under the 'spawn' start method (default on macOS/Windows).
    """
    _bn_analyze_file(item)
    return item[0]


def _analyze_with_per_file_progress(
    audio_input: str,
    output: str,
    *,
    on_file_done,  # callable(file_path_str: str) -> None
    cancel_check,  # callable() -> bool
    **analyze_kwargs,
) -> None:
    """Drop-in replacement for birdnet_analyzer.analyze with per-file callbacks.

    Why: the upstream analyze() runs Pool.map_async(...).wait(), which
    blocks for the whole batch and offers no progress hook, so the UI
    bar sat at 0% for the entire run. This function mirrors the upstream
    body but uses Pool.imap_unordered so we get one yield per completed
    file. After each yield we call on_file_done(path) for a progress
    snapshot and re-check cancel_check() so a Stop click takes effect
    within one file rather than one campaign.

    When: called from _run_campaign, once per week_dir in location mode or
    once for the whole campaign in list / global mode.
    """
    flist = _set_params(
        audio_input=audio_input,
        output=output,
        min_conf=analyze_kwargs.get("min_conf", 0.25),
        custom_classifier=analyze_kwargs.get("classifier"),
        lat=analyze_kwargs.get("lat", -1),
        lon=analyze_kwargs.get("lon", -1),
        week=analyze_kwargs.get("week", -1),
        slist=analyze_kwargs.get("slist"),
        sensitivity=analyze_kwargs.get("sensitivity", 1.0),
        locale=analyze_kwargs.get("locale", "en"),
        overlap=analyze_kwargs.get("overlap", 0),
        fmin=analyze_kwargs.get("fmin", 0),
        fmax=analyze_kwargs.get("fmax", 15000),
        audio_speed=analyze_kwargs.get("audio_speed", 1.0),
        bs=analyze_kwargs.get("batch_size", 1),
        combine_results=False,
        rtype=analyze_kwargs.get("rtype", "csv"),
        skip_existing_results=analyze_kwargs.get("skip_existing_results", False),
        sf_thresh=analyze_kwargs.get("sf_thresh", 0.03),
        top_n=analyze_kwargs.get("top_n"),
        merge_consecutive=analyze_kwargs.get("merge_consecutive", 1),
        threads=analyze_kwargs.get("threads", 8),
        labels_file=birdnet_cfg.LABELS_FILE,
        additional_columns=analyze_kwargs.get("additional_columns"),
        use_perch=False,
    )

    if birdnet_cfg.CPU_THREADS < 2 or len(flist) < 2:
        for item in flist:
            if cancel_check():
                raise CancelledError()
            _bn_analyze_file(item)
            on_file_done(item[0])
    else:
        with Pool(birdnet_cfg.CPU_THREADS) as pool:
            for finished_path in pool.imap_unordered(_analyze_file_with_path, flist):
                on_file_done(finished_path)
                if cancel_check():
                    pool.terminate()
                    raise CancelledError()

    save_analysis_params(os.path.join(birdnet_cfg.OUTPUT_PATH, birdnet_cfg.ANALYSIS_PARAMS_FILENAME))


def _build_location_slists(
    lat: float,
    lon: float,
    week_dirs: list[Path],
    must_haves: list[str],
    output_dir: Path,
    campaign_name: str,
) -> tuple[dict[int, str], str | None, list[str]]:
    """Merge BirdNET's location-derived species list with user must-haves.

    birdnet_analyzer treats slist and lat/lon as mutually exclusive: a slist
    file replaces location filtering entirely. To honor a must-have list in
    location mode we compute the geographic list ourselves per week, union
    the must-haves on top, and feed the union back as a slist file.

    Returns a {week: slist_path} map (per-week deployments), a single slist
    path (no week_NN folders), and any warnings. On failure both are empty
    so the caller falls back to plain location filtering.
    """
    from birdnet_analyzer.species.utils import get_species_list  # type: ignore[import]

    week_slists: dict[int, str] = {}
    single: str | None = None
    warnings: list[str] = []

    def _merge(week: int) -> list[str]:
        geo = get_species_list(lat, lon, week, threshold=0.03)
        seen = set(geo)
        return [*geo, *(s for s in must_haves if s not in seen)]

    try:
        if week_dirs:
            weeks = sorted({w for d in week_dirs if (w := _week_from_path(d)) is not None})
            for w in weeks:
                path = output_dir / f"{campaign_name}-species-list-week-{w:02d}-input.txt"
                path.write_text("\n".join(_merge(w)) + "\n", encoding="utf-8")
                week_slists[w] = str(path)
        else:
            path = output_dir / f"{campaign_name}-species-list-input.txt"
            path.write_text("\n".join(_merge(-1)) + "\n", encoding="utf-8")
            single = str(path)
    except Exception as exc:  # noqa: BLE001
        msg = f"Failed to merge must-have species with location list: {exc}"
        print(f"[birdnet] {msg}", file=sys.stderr)
        warnings.append(msg)

    return week_slists, single, warnings


def _run_campaign(
    ci: CampaignRunInput,
    output_dir: Path,
    settings: AnalysisSettings,
    preferred_lang: str,
    audio_root: Path,
    progress: AnalysisProgress,
    campaign_index: int,
    total_campaigns: int,
) -> CampaignRunResult:
    """Run a single campaign through every phase and return its result row.

    Sequencing:
      1. preparing      - resolve species filter mode (location vs list vs
                          global) and write the species list file when
                          the user provided one.
      2. analyzing      - delegate to _analyze_with_per_file_progress;
                          this is the long phase the user mostly waits on.
      3. parsing        - read every *.BirdNET.results.csv birdnet wrote
                          and stream rows into <campaign>-detections.csv.
                          In location mode, also write geographic species
                          lists when lat/lon are set.
      4. done           - terminal snapshot so the bar reaches 100%.

    Called once per campaign by BirdnetRunner.run. Raises CancelledError
    when progress.is_cancelled() flips to True at any of the checked points.
    """
    campaign_name = ci.name
    t0 = time.monotonic()
    output_dir.mkdir(parents=True, exist_ok=True)

    _emit_progress(
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
    week_slists: dict[int, str] = {}
    warnings: list[str] = []
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

    # Split user-supplied must-have species-list blob into stripped, non-empty lines.
    must_haves = [
        line.strip()
        for line in (ci.must_have_species_text or "").splitlines()
        if line.strip()
    ]
    # When in location mode: merge the must_haves on top of the location-derived list
    if must_haves and lat is not None and lon is not None:
        week_slists, merged_single, merge_warnings = _build_location_slists(
            lat, lon, week_dirs, must_haves, output_dir, campaign_name
        )
        warnings.extend(merge_warnings)
        if merged_single is not None:
            slist = merged_single

    # When a species list file drives the run (list mode or a must-have
    # merge), birdnet derives species from the file, not lat/lon.
    slist_active = slist is not None or bool(week_slists)

    wav_count = _count_audio_files(ci.folder)
    num_threads = min(os.cpu_count() or 4, 8)
    analyze_kwargs = {
        "min_conf": settings.min_conf,
        "slist": slist,
        "lat": lat if lat is not None and not slist_active else -1,
        "lon": lon if lon is not None and not slist_active else -1,
        "overlap": settings.overlap,
        "top_n": None,
        "rtype": "csv",
        "combine_results": False,
        "threads": num_threads,
        "locale": "en",  # birdnet_analyzer outputs English labels; non-English locales are mapped post-hoc
    }

    done_counter = {"n": 0}

    def _on_file_done(fpath: str) -> None:
        done_counter["n"] += 1
        finished = Path(fpath)
        # Path relative to the campaign root surfaces the ARU and (in
        # location mode) the week folder, e.g. "MSD-109/week_08/x.wav".
        try:
            detail = finished.relative_to(ci.folder).as_posix()
        except ValueError:
            detail = finished.name
        _emit_progress(
            progress,
            campaign=campaign_name,
            campaign_index=campaign_index,
            total_campaigns=total_campaigns,
            files_done=done_counter["n"],
            files_total=wav_count,
            phase="analyzing",
            phase_detail=detail,
        )

    _emit_progress(
        progress,
        campaign=campaign_name,
        campaign_index=campaign_index,
        total_campaigns=total_campaigns,
        files_done=0,
        files_total=wav_count,
        phase="analyzing",
    )

    if week_dirs:
        for week_dir in week_dirs:
            if progress.is_cancelled():
                raise CancelledError()
            dir_week = _week_from_path(week_dir)
            if dir_week is None:
                continue
            week_out = output_dir / week_dir.relative_to(ci.folder)
            week_out.mkdir(parents=True, exist_ok=True)
            week_kwargs = analyze_kwargs
            if week_slists:
                week_kwargs = {**analyze_kwargs, "slist": week_slists.get(dir_week)}
            _analyze_with_per_file_progress(
                str(week_dir),
                str(week_out),
                on_file_done=_on_file_done,
                cancel_check=progress.is_cancelled,
                week=dir_week,
                **week_kwargs,
            )
    else:
        _analyze_with_per_file_progress(
            str(ci.folder),
            str(output_dir),
            on_file_done=_on_file_done,
            cancel_check=progress.is_cancelled,
            week=week,
            **analyze_kwargs,
        )

    _emit_progress(
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

    if not result_csvs:
        return CampaignRunResult(
            campaign_name=campaign_name,
            output_dir=output_dir,
            detections_csv=detections_csv,
            species_list_txt=None,
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

    # Export geographic species list(s) in location mode.
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

    _emit_progress(
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
        species_list_txt=species_list_txt,
        detection_count=len(detections),
        wav_count=wav_count,
        aru_count=len({row["ARU"] for row in detections}),
        elapsed=time.monotonic() - t0,
        warnings=tuple(warnings),
    )


class BirdnetRunner:
    """AnalysisRunner implementation for BirdNET.

    Handles model prewarm, the Pool-based per-file analysis loop, CSV
    parsing, and the per-campaign <campaign>-detections.csv. run() iterates
    the campaigns and normalises progress across campaign boundaries via
    _RunGlobalProgress.
    """

    def count_audio_files(self, campaign_dir: Path) -> int:
        return _count_audio_files(campaign_dir)

    def available_locales(self) -> list[str]:
        return _get_available_locales()

    def run(
        self,
        *,
        campaigns: list[CampaignRunInput],
        output_base: Path,
        settings: AnalysisSettings,
        preferred_lang: str,
        audio_root: Path,
        progress: AnalysisProgress,
    ) -> AnalysisRunResult:
        _prewarm_model()
        output_base.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()

        per_campaign_totals = [_count_audio_files(ci.folder) for ci in campaigns]
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
            results.append(
                _run_campaign(ci, camp_out, settings, preferred_lang, audio_root, run_progress, i, total)
            )
            files_completed += ci_total

        return AnalysisRunResult(
            campaigns=tuple(results),
            elapsed=time.monotonic() - t0,
        )
