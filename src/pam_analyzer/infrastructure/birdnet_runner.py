"""BirdNET v2.4 infrastructure adapter.

BirdnetRunner is an AnalysisRunner backed by BirdNET v2.4 (TFLite),
loaded via the birdnet>=0.2 library. Audio I/O, 3 s window framing,
batched inference, sigmoid scoring, and the confidence threshold all live
inside the lib's predict_session pipeline; this module is responsible for
the per-campaign loop, species-filter resolution, locale-aware row
shaping, and the campaign-detections-BirdNET-2.4.csv output.

Sequencing per campaign mirrors PerchRunner so the worker / progress code
does not need to know which runner is active:

    BirdnetRunner.run(...)
      -> birdnet.load("acoustic", "2.4", "tf", lang="en_us")  once
      -> _run_campaign(...) per campaign
           -> _emit_progress("preparing")
           -> resolve per-week allow-list (geo whitelist + must-haves,
              or fixed LIST set, or no filter)
           -> in LOCATION mode, write the per-week species list TXT
              files alongside the detections CSV (for the user's records)
           -> _emit_progress("analyzing")
           -> model.predict_session(...)         single session per campaign
              -> session.run(all_files)
              -> progress_callback maps stats to AnalysisProgressSnapshot
                 and triggers session.cancel() when is_cancelled() flips
           -> _emit_progress("parsing")
           -> walk structured array; post-filter by per-week allow-list;
              assign per-(file, chunk) rank; write
              campaign-detections-BirdNET-2.4.csv
           -> _emit_progress("done")

The lib's `species_name` in result rows is in 'Scientific_Common' format
because we load the model with lang='en_us'. We split each entry to get
the scientific name (canonical axis for the allow-list check) and the
English common name; other locales come from locale_label_map().
"""

from __future__ import annotations

import csv
import logging
import re
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

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
from .birdnet_lib import available_locales as _available_locales
from .birdnet_lib import (
    locale_label_map,
    normalize_lang_code,
    region_species_scientific,
)


def _count_audio_files(campaign_dir: Path) -> int:
    return sum(
        1 for f in campaign_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in paths.AUDIO_EXTENSIONS
    )


def _list_audio_files(campaign_dir: Path) -> list[Path]:
    return sorted(
        f for f in campaign_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in paths.AUDIO_EXTENSIONS
    )


def _parse_recording_time(stem: str) -> datetime | None:
    """Pull a 'YYYYMMDD_HHMMSS' timestamp out of an audio filename."""
    match = re.search(r"(\d{8}_\d{6})", stem)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
        except ValueError:
            pass
    return None


def _week_from_path(path: Path) -> int | None:
    """Extract the ISO week number from a 'week_NN' path segment, or None."""
    for part in path.parts:
        if part.startswith("week_"):
            try:
                return int(part.split("_", 1)[1])
            except (IndexError, ValueError):
                pass
    return None


def _split_sci_common(species_name: str) -> tuple[str, str]:
    """Split a 'Scientific_Common' label entry into (sci, common)."""
    sci, _, common = species_name.partition("_")
    return sci, common


def _parse_species_lines(text: str) -> frozenset[str]:
    """Parse a user-supplied species blob into a set of scientific names.

    Accepts plain Latin names or 'Scientific_Common' entries copied from a
    BirdNET-style species list, matching what the old slist file parser
    accepted.
    """
    return frozenset(
        line.split("_", 1)[0].strip()
        for line in text.splitlines()
        if line.strip()
    )


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


class _RunGlobalProgress:
    """Rewrite per-campaign snapshots so a multi-campaign run shows monotonic progress.

    Without this adapter the UI progress bar would refill to 0% at every
    campaign boundary. We carry a baseline offset and rewrite files_done /
    files_total to run-global counts, leaving phase and campaign untouched
    so the label still tells the user which campaign is active.
    """

    def __init__(self, inner: AnalysisProgress, run_total: int) -> None:
        self._inner = inner
        self._run_total = run_total
        self._baseline = 0

    def start_campaign(self, files_done_so_far: int) -> None:
        self._baseline = files_done_so_far

    def report(self, snapshot: AnalysisProgressSnapshot) -> None:
        global_done = min(self._baseline + snapshot.files_done, self._run_total)
        self._inner.report(
            replace(snapshot, files_done=global_done, files_total=self._run_total)
        )

    def is_cancelled(self) -> bool:
        return self._inner.is_cancelled()


def _build_allowed_lookup(
    ci: CampaignRunInput, wav_files: list[Path]
) -> tuple[Callable[[Path], frozenset[str] | None], float | None, float | None, dict[int, frozenset[str]]]:
    """Per-file species-allow-list resolver and the (lat, lon) for the run.

    The fourth tuple element is the per-week regional set (LOCATION mode
    only, empty dict otherwise). The runner uses it to write the
    <campaign>-species-list-week-NN.txt files the previous birdnet_analyzer
    flow produced as a byproduct.

    Three regimes:

    - GLOBAL or empty input -> callable always returns None, meaning
      "no filter; keep every row".
    - LIST with `species_list_text` -> callable returns one fixed
      frozenset for any path. Same allow-list applies to every week.
    - LOCATION with lat/lon -> callable looks up the file's week from its
      path and returns the precomputed regional set for that week, with
      must-have species unioned on top so a user-added bird never gets
      filtered out by the geo model.
    """
    if ci.mode == FilterMode.LIST and ci.species_list_text:
        fixed = _parse_species_lines(ci.species_list_text)
        return (lambda _p: fixed), None, None, {}

    if ci.mode == FilterMode.LOCATION and ci.location is not None:
        lat = ci.location.latitude
        lon = ci.location.longitude
        must_haves = _parse_species_lines(ci.must_have_species_text or "")
        weeks_present: set[int] = set()
        for f in wav_files:
            w = _week_from_path(f)
            weeks_present.add(w if w is not None else -1)
        per_week_geo: dict[int, frozenset[str]] = {
            w: region_species_scientific(lat, lon, w) for w in weeks_present
        }
        per_week_allowed: dict[int, frozenset[str]] = {
            w: (geo | must_haves) if must_haves else geo
            for w, geo in per_week_geo.items()
        }

        def lookup(path: Path) -> frozenset[str] | None:
            w = _week_from_path(path)
            return per_week_allowed.get(w if w is not None else -1)

        return lookup, lat, lon, per_week_geo

    return (lambda _p: None), None, None, {}


def _write_species_list_files(
    output_dir: Path,
    campaign_name: str,
    per_week_geo: dict[int, frozenset[str]],
) -> Path | None:
    """Write the per-week geographic species list as plain text.

    Produces <campaign>-species-list[-week-NN].txt files matching the
    naming the previous birdnet_analyzer-based runner wrote, so any
    user-facing scripts that consume them keep working. When weeks are
    present we write one file per week. When no week_NN segments exist
    the per_week_geo dict has a single key (-1) and we write a single
    <campaign>-species-list.txt; that path is returned for inclusion in
    CampaignRunResult.species_list_txt.
    """
    if not per_week_geo:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    single_path: Path | None = None
    weeks = sorted(per_week_geo)
    if weeks == [-1]:
        single_path = output_dir / f"{campaign_name}-species-list.txt"
        single_path.write_text(
            "\n".join(sorted(per_week_geo[-1])) + "\n", encoding="utf-8"
        )
        return single_path

    for w, species in per_week_geo.items():
        if w == -1:
            # Files without a week_NN segment in a campaign that does have
            # week folders are rare; fall back to a 'no-week' file so the
            # list is still preserved.
            (output_dir / f"{campaign_name}-species-list.txt").write_text(
                "\n".join(sorted(species)) + "\n", encoding="utf-8"
            )
        else:
            (output_dir / f"{campaign_name}-species-list-week-{w:02d}.txt").write_text(
                "\n".join(sorted(species)) + "\n", encoding="utf-8"
            )
    return None


def _build_progress_callback(
    progress: AnalysisProgress,
    *,
    campaign: str,
    campaign_index: int,
    total_campaigns: int,
    files_total: int,
    session_ref: list[Any],
) -> Callable[[Any], None]:
    """Bridge AcousticProgressStats to our AnalysisProgressSnapshot.

    Approximates files_done from `stats.progress_pct` because the lib
    reports progress in segments processed, not files. The lib's own ETA
    string is forwarded as `phase_detail` so the UI can render a 'time
    remaining' string in place of the per-file path we no longer surface.

    The callback also doubles as the cancellation hook: when Stop is
    clicked, is_cancelled() flips True and we tell the lib's session to
    wind down, which makes session.run raise RuntimeError on exit.
    """

    def cb(stats: Any) -> None:
        session = session_ref[0]
        if progress.is_cancelled() and session is not None:
            try:
                session.cancel()
            except RuntimeError:
                pass
            return
        files_done = min(
            files_total, int(round(stats.progress_pct / 100.0 * files_total))
        )
        eta = stats.est_remaining_time_hhmmss
        _emit_progress(
            progress,
            campaign=campaign,
            campaign_index=campaign_index,
            total_campaigns=total_campaigns,
            files_done=files_done,
            files_total=files_total,
            phase="analyzing",
            phase_detail=(f"ETA {eta}" if eta else None),
        )

    return cb


def _run_campaign(
    ci: CampaignRunInput,
    output_dir: Path,
    settings: AnalysisSettings,
    preferred_lang: str,
    audio_root: Path,
    progress: AnalysisProgress,
    campaign_index: int,
    total_campaigns: int,
    model: Any,
) -> CampaignRunResult:
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

    wav_files = _list_audio_files(ci.folder)
    wav_count = len(wav_files)

    detections_csv = paths.campaign_csv_for_model(
        output_dir.parent, campaign_name, BirdnetRunner.model_key
    )

    # Resolve the species filter before opening the inference session: in
    # LOCATION mode this pre-warms the geo model and computes per-week
    # whitelists, so any geo lookup cost is paid during 'preparing'.
    allowed_for, lat, lon, per_week_geo = _build_allowed_lookup(ci, wav_files)

    # Preserve the species-list TXT side outputs the previous flow wrote.
    # These are user-facing artifacts independent of the detections CSV.
    species_list_txt = _write_species_list_files(output_dir, campaign_name, per_week_geo)

    run_context = {
        "Lat": lat if lat is not None else "",
        "Lon": lon if lon is not None else "",
        "Species_List": "",
        "Min_Conf": settings.min_conf,
        "Model": BirdnetRunner.model_key,
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

    if wav_count == 0:
        with open(detections_csv, "w", newline="", encoding="utf-8") as outfile:
            csv.DictWriter(outfile, fieldnames=fieldnames).writeheader()
        _emit_progress(
            progress,
            campaign=campaign_name,
            campaign_index=campaign_index,
            total_campaigns=total_campaigns,
            files_done=0,
            files_total=0,
            phase="done",
        )
        return CampaignRunResult(
            campaign_name=campaign_name,
            output_dir=output_dir,
            detections_csv=detections_csv,
            species_list_txt=species_list_txt,
            detection_count=0,
            wav_count=0,
            aru_count=0,
            elapsed=time.monotonic() - t0,
            model_key=BirdnetRunner.model_key,
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

    session_ref: list[Any] = [None]
    on_stats = _build_progress_callback(
        progress,
        campaign=campaign_name,
        campaign_index=campaign_index,
        total_campaigns=total_campaigns,
        files_total=wav_count,
        session_ref=session_ref,
    )

    # custom_species_list is intentionally None: we apply the per-week
    # allow-list as a post-filter on result rows below. The lib's mask is
    # session-bound and cannot change between weeks, so a single global
    # session plus our row-level check yields the same filtered output as
    # one-session-per-week without the per-week model warmup.
    with model.predict_session(
        default_confidence_threshold=settings.min_conf,
        custom_species_list=None,
        overlap_duration_s=settings.overlap,
        top_k=None,
        apply_sigmoid=True,
        sigmoid_sensitivity=1.0,
        n_producers=1,
        n_workers=None,
        batch_size=8,
        show_stats="progress",
        progress_callback=on_stats,
        max_n_files=wav_count,
        device="CPU",
    ) as session:
        session_ref[0] = session
        try:
            result = session.run(wav_files)
        except RuntimeError as exc:
            if progress.is_cancelled():
                raise CancelledError() from exc
            raise

    if progress.is_cancelled():
        raise CancelledError()

    _emit_progress(
        progress,
        campaign=campaign_name,
        campaign_index=campaign_index,
        total_campaigns=total_campaigns,
        files_done=wav_count,
        files_total=wav_count,
        phase="parsing",
    )

    preferred_lang_map = locale_label_map(preferred_lang)
    locale_maps = {loc: locale_label_map(loc) for loc in settings.locales}

    detection_count = 0
    filtered_count = 0
    aru_set: set[str] = set()

    arr = result.to_structured_array()

    with open(detections_csv, "w", newline="", encoding="utf-8") as outfile:
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()

        # Rank is recomputed per (file, chunk_start) over rows that survive
        # the per-week allow-list. The lib already sorts rows by (file_idx
        # asc, chunk_idx asc, confidence desc); dropping out-of-region rows
        # preserves that order, so the streaming counter below assigns the
        # correct rank without re-sorting.
        prev_key: tuple[str, float] | None = None
        rank = 0
        for row in arr:
            file_path = Path(str(row["input"]))
            start_t = float(row["start_time"])
            end_t = float(row["end_time"])
            species_name = str(row["species_name"])
            conf = float(row["confidence"])

            sci, common_en = _split_sci_common(species_name)

            allowed = allowed_for(file_path)
            if allowed is not None and sci not in allowed:
                filtered_count += 1
                continue

            try:
                aru = file_path.relative_to(ci.folder).parts[0]
            except (ValueError, IndexError):
                aru = ""
            aru_set.add(aru)

            try:
                file_rel = file_path.relative_to(audio_root).as_posix()
            except ValueError:
                file_rel = file_path.as_posix()

            recording_time = _parse_recording_time(file_path.stem)
            file_week = _week_from_path(file_path)

            key = (str(file_path), start_t)
            if key != prev_key:
                prev_key = key
                rank = 1
            else:
                rank += 1

            # Preferred-language common name. Fall back to the lib's en_us
            # common name if the locale lookup misses (e.g. a recently
            # added species not yet translated in the user's lang).
            common = preferred_lang_map.get(sci, common_en or sci)
            locale_names = {
                f"Species_{loc}": (
                    common_en if loc == "en_us" else locale_maps[loc].get(sci, "")
                )
                for loc in settings.locales
            }

            writer.writerow({
                "Campaign": campaign_name,
                "ARU": aru,
                "Start_Time": f"{start_t:.1f}",
                "End_Time": f"{end_t:.1f}",
                "Scientific_Name": sci,
                "Species": common,
                **locale_names,
                "Confidence": f"{conf:.4f}",
                "Rank": rank,
                "File": file_rel,
                "Recording_Time": str(recording_time) if recording_time else "",
                "Week": file_week if file_week is not None else -1,
                **run_context,
                "Verified": "",
                "Corrected_Species": "",
                "Comment": "",
            })
            detection_count += 1

    if filtered_count:
        logging.info(
            "birdnet: per-week species filter dropped %d row(s); %d kept",
            filtered_count,
            detection_count,
        )

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
        detection_count=detection_count,
        wav_count=wav_count,
        aru_count=len(aru_set),
        elapsed=time.monotonic() - t0,
        model_key=BirdnetRunner.model_key,
    )


class BirdnetRunner:
    """AnalysisRunner implementation backed by BirdNET v2.4 (TFLite).

    Loads the model once via birdnet.load('acoustic', '2.4', 'tf') and
    reuses it across campaigns and files. The lib handles 48 kHz audio
    I/O, 3 s window framing, batched TFLite inference, sigmoid scoring,
    and the confidence threshold; this runner adds the per-campaign loop,
    species-filter resolution, locale-aware row shaping, and writes
    <campaign>/<campaign>-detections-BirdNET-2.4.csv plus the per-week
    species-list TXT files.
    """

    model_key = "BirdNET-2.4"

    def count_audio_files(self, campaign_dir: Path) -> int:
        return _count_audio_files(campaign_dir)

    def available_locales(self) -> list[str]:
        return list(_available_locales())

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
        import birdnet

        output_base.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()

        # Project files saved under birdnet_analyzer used short locale
        # codes ('en', 'de'); the new lib uses 'en_us' / 'en_uk' / 'de'.
        # Normalise so a stale 'en' degrades to 'en_us' silently.
        preferred_lang = normalize_lang_code(preferred_lang)

        # Load with en_us so result rows carry English common names in
        # the 'Sci_Common' species_name string. Other locales come from
        # locale_label_map() lookups in the row loop.
        model = birdnet.load("acoustic", "2.4", "tf", lang="en_us")

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
            try:
                results.append(
                    _run_campaign(
                        ci,
                        camp_out,
                        settings,
                        preferred_lang,
                        audio_root,
                        run_progress,
                        i,
                        total,
                        model,
                    )
                )
            except CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logging.exception("birdnet: campaign %s failed: %s", ci.name, exc)
                raise
            files_completed += ci_total

        return AnalysisRunResult(
            campaigns=tuple(results),
            elapsed=time.monotonic() - t0,
        )
