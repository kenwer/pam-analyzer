"""Perch v2 infrastructure adapter.

PerchRunner is an AnalysisRunner backed by Google's Perch v2 SavedModel,
loaded via the birdnet>=0.2 library. Most of the heavy lifting (audio I/O,
resampling, 5 s window framing, batched inference, threshold filtering)
lives inside the lib's predict_session pipeline; this module is responsible
for the per-campaign loop, the species-filter resolution, and shaping the
result into our existing detections CSV schema.

Sequencing per campaign:

    PerchRunner.run(...)
      -> birdnet.load_perch_v2(...)               once at the top
      -> _run_campaign(...) per campaign
           -> _emit_progress("preparing")
           -> resolve species filter (LIST text, LOCATION whitelist, or None)
           -> _emit_progress("analyzing")
           -> model.predict_session(...)          one session per campaign
              -> session.run(all_files)           lib walks files in workers
              -> progress_callback maps stats to AnalysisProgressSnapshot
                 and triggers session.cancel() when is_cancelled() flips
           -> _emit_progress("parsing")
           -> walk structured array; assign per-(file, chunk) rank;
              write campaign-detections-Perch-2.0.csv
           -> _emit_progress("done")

Cancellation: the progress callback the lib invokes during inference
checks progress.is_cancelled() each tick and calls session.cancel(), which
makes the lib raise RuntimeError on exit. We translate that into our
CancelledError. Mid-campaign granularity is limited to whatever cadence
the lib's progress callback fires at.
"""

from __future__ import annotations

import csv
import logging
import math
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

# Perch v2's class head emits positive logits everywhere: pure silence
# sits around +4.5, and real ambient noise (wind, distant traffic) sits
# higher still. Without an offset, every 5 s window emits its top-k
# species at ~0.99 sigmoid even when no bird is calling.
#
# The offset was tuned by cross-comparison against BirdNET v2.4 on the
# Camp1 campaign at min_conf=0.2 (843 BirdNET rows, 1547 Perch rows at
# OFFSET=11.0). Initial calibration looked at where BirdNET and Perch
# agreed on species in the same window; refined after the user spot-
# checked the borderline detections and confirmed that low-confidence
# Perch rows for at least Corvus corone are real distant/quiet calls,
# not noise.
#
# The 11.2 setting was chosen because per-species recall vs BirdNET
# shows a sharp cliff between 11.2 and 11.3: Corvus corone holds at
# 100% up to 11.2 then collapses to 87% at 11.3 and 69% at 11.5. That
# cliff marks the boundary between genuine quiet calls and noise.
# Quantitatively: 1391 Perch rows (1.65x BirdNET), 97.4% retention of
# cross-validated agreements, 10% reduction vs OFFSET=11.0's 1547 rows.
#
# BirdNET v2.4 does not need this: its logits are centred around 0.
_PERCH_LOGIT_OFFSET = 11.2


def _perch_logit_threshold(min_conf: float) -> float:
    """Map a probability-space threshold to Perch's logit space."""
    p = min(max(min_conf, 1e-6), 1.0 - 1e-6)
    return math.log(p / (1.0 - p)) + _PERCH_LOGIT_OFFSET


def _perch_logit_to_prob(logit: float) -> float:
    """Calibrated probability for a Perch v2 logit, inverse of the threshold map."""
    return 1.0 / (1.0 + math.exp(-(logit - _PERCH_LOGIT_OFFSET)))


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


def _parse_species_lines(text: str) -> frozenset[str]:
    """Parse a user-supplied species blob into a set of scientific names.

    Accepts either plain Latin names ('Parus major') or 'Scientific_Common'
    entries copied from a BirdNET-style species list, so users can paste
    either format without converting. Everything after a `#` on a line is
    treated as a comment, so users can annotate their lists or paste back
    lines this runner emitted with `  # must-have` markers.
    """
    out: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        out.add(line.split("_", 1)[0].strip())
    return frozenset(out)


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
    """Translate per-campaign snapshots to run-global counts.

    Multi-campaign runs would otherwise see the progress bar reset to 0% at
    every campaign boundary; this adapter rewrites files_done/files_total
    so the UI bar advances monotonically across the whole run while phase
    and phase_detail still tell the user which campaign is active.
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
) -> tuple[
    Callable[[Path], frozenset[str] | None],
    float | None,
    float | None,
    dict[int, frozenset[str]],
    frozenset[str],
]:
    """Per-file species-allow-list resolver, plus the (lat, lon) for the run.

    Returns a callable `allowed_for(path)` that the row loop calls once per
    detection to decide whether to keep it, plus the per-week applied set
    (geo + must-haves) and the must-have subset, both used by the
    species-list TXT writer. Three regimes:

    - GLOBAL or empty input -> callable always returns None, meaning
      "no filter; keep every row". The caller short-circuits the check.
    - LIST with `species_list_text` -> callable returns a single fixed
      frozenset for any path. Same allow-list applies to every week.
    - LOCATION with lat/lon -> callable looks up the file's week from its
      path and returns the precomputed regional set for that week. Must-
      have species are unioned on top so a user-added bird never gets
      filtered out even if BirdNET's geo model considers it implausible.

    The per-week sets are computed lazily as new weeks are encountered and
    memoised, so the geo model gets called at most once per distinct week
    in the campaign.
    """
    if ci.mode == FilterMode.LIST and ci.species_list_text:
        fixed = _parse_species_lines(ci.species_list_text)
        return (lambda _p: fixed), None, None, {}, frozenset()

    if ci.mode == FilterMode.LOCATION and ci.location is not None:
        lat = ci.location.latitude
        lon = ci.location.longitude
        must_haves = _parse_species_lines(ci.must_have_species_text or "")
        # Pre-warm the per-week cache from the wav file list, so any geo
        # downloads happen during the 'preparing' phase rather than mid-
        # inference. Week=-1 stands in for files with no 'week_NN' segment.
        weeks_present: set[int] = set()
        for f in wav_files:
            w = _week_from_path(f)
            weeks_present.add(w if w is not None else -1)
        per_week: dict[int, frozenset[str]] = {
            w: region_species_scientific(lat, lon, w) | must_haves
            for w in weeks_present
        }

        def lookup(path: Path) -> frozenset[str] | None:
            w = _week_from_path(path)
            return per_week.get(w if w is not None else -1)

        return lookup, lat, lon, per_week, must_haves

    return (lambda _p: None), None, None, {}, frozenset()


def _write_species_list_files(
    output_dir: Path,
    campaign_name: str,
    per_week_allowed: dict[int, frozenset[str]],
    must_haves: frozenset[str],
) -> Path | None:
    """Write the per-week applied species list as plain text.

    Each file contains the merged list (geo + must-haves) the runner
    actually filtered against, with `  # must-have` appended to lines
    whose species came from the user's must-have input. One file per
    week when week_NN folders are present, or a single
    <campaign>-species-list.txt when they are not; that single-file
    path is returned for inclusion in CampaignRunResult.species_list_txt.
    """
    if not per_week_allowed:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    weeks = sorted(per_week_allowed)
    if weeks == [-1]:
        single_path = output_dir / f"{campaign_name}-species-list.txt"
        single_path.write_text(_format_species_lines(per_week_allowed[-1], must_haves), encoding="utf-8")
        return single_path

    for w, species in per_week_allowed.items():
        if w == -1:
            (output_dir / f"{campaign_name}-species-list.txt").write_text(
                _format_species_lines(species, must_haves), encoding="utf-8"
            )
        else:
            (output_dir / f"{campaign_name}-species-list-week-{w:02d}.txt").write_text(
                _format_species_lines(species, must_haves), encoding="utf-8"
            )
    return None


def _format_species_lines(species: frozenset[str], must_haves: frozenset[str]) -> str:
    """Format one species list with a `# must-have` marker for user-added entries."""
    lines = []
    for name in sorted(species):
        if name in must_haves:
            lines.append(f"{name}  # must-have")
        else:
            lines.append(name)
    return "\n".join(lines) + "\n"


def _build_progress_callback(
    progress: AnalysisProgress,
    *,
    campaign: str,
    campaign_index: int,
    total_campaigns: int,
    files_total: int,
    session_ref: list[Any],
) -> Callable[[Any], None]:
    """Bridge AcousticProgressStats from the lib to our snapshot port.

    Approximates files_done from `stats.progress_pct` because the new
    library reports progress in segments processed, not files. The lib's
    own estimated time remaining is forwarded verbatim as `phase_detail`
    so the UI can render an ETA in place of the per-file path we dropped
    when moving to single-session inference.

    The callback also serves as our cancellation hook: when the user
    clicks Stop, is_cancelled() flips True and we tell the lib's session
    to wind down, which makes session.run raise RuntimeError on exit.
    """

    def cb(stats: Any) -> None:
        session = session_ref[0]
        if progress.is_cancelled() and session is not None:
            try:
                session.cancel()
            except RuntimeError:
                # Session already torn down; nothing to do.
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
        output_dir.parent, campaign_name, PerchRunner.model_key
    )

    # Resolve the species allow-list before opening the session: in
    # LOCATION mode this triggers the geo model download / lookup once per
    # week present in the campaign, which we want paid during the
    # 'preparing' phase rather than mid-inference.
    allowed_for, lat, lon, per_week_allowed, must_haves = _build_allowed_lookup(ci, wav_files)

    # Write the applied per-week allow-list (geo + must-haves) alongside
    # the detections so users can inspect exactly what the model was
    # asked to consider.
    species_list_txt = _write_species_list_files(
        output_dir, campaign_name, per_week_allowed, must_haves
    )

    run_context = {
        "Lat": lat if lat is not None else "",
        "Lon": lon if lon is not None else "",
        "Species_List": "",
        "Min_Conf": settings.min_conf,
        "Model": PerchRunner.model_key,
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
        # Write an empty CSV so downstream readers find the file with the
        # expected header even when the campaign had no audio.
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
            model_key=PerchRunner.model_key,
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

    # session_ref is a one-slot list so the progress callback (constructed
    # before the session exists) can reach the session via closure once we
    # bind it inside the `with` block below.
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
    # session-bound (cannot change between weeks), so a single global
    # session plus our row-level check gives the same filtered output as
    # one-session-per-week would, without paying the SavedModel reload
    # cost on every week boundary.
    #
    # apply_sigmoid=False follows the library's own default for Perch v2.
    # The model emits raw class logits and we threshold in logit space via
    # _perch_logit_threshold(); rows are converted back to a calibrated
    # probability below before being written, so CSV Confidence stays in
    # 0-1 and matches the BirdNET runner's units.
    #
    # top_k=5 caps per-segment emissions. Perch is multi-label across
    # 14,795 classes; without a top-K cap a single campaign produced ~300k
    # rows, most of them low-quality co-activations. 5 matches the lib's
    # own default and the 1-3 species per segment a well-tuned acoustic
    # model typically surfaces.
    with model.predict_session(
        default_confidence_threshold=_perch_logit_threshold(settings.min_conf),
        custom_species_list=None,
        overlap_duration_s=settings.overlap,
        top_k=5,
        apply_sigmoid=False,
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

        # Rank is recomputed per (file, chunk_start) group over the rows
        # that survive the per-week allow-list. The library already sorts
        # rows by (file_idx asc, chunk_idx asc, confidence desc); dropping
        # out-of-region rows preserves that order, so a streaming pass that
        # resets on key change yields the right rank without re-sorting.
        prev_key: tuple[str, float] | None = None
        rank = 0
        for row in arr:
            file_path = Path(str(row["input"]))
            start_t = float(row["start_time"])
            end_t = float(row["end_time"])
            sci = str(row["species_name"])
            # The lib returned a raw logit because apply_sigmoid=False;
            # convert here so CSV Confidence stays a 0-1 probability and
            # matches the BirdNET runner's units. See _PERCH_LOGIT_OFFSET.
            conf = _perch_logit_to_prob(float(row["confidence"]))

            # Per-week / per-list post-filter. None means 'no filter for
            # this file' (GLOBAL mode, or LOCATION mode where the file's
            # week has no recognised geo whitelist).
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

            common = preferred_lang_map.get(sci, sci)
            locale_names = {
                f"Species_{loc}": locale_maps[loc].get(sci, "")
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
            "perch: per-week species filter dropped %d row(s); %d kept",
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
        model_key=PerchRunner.model_key,
    )


class PerchRunner:
    """AnalysisRunner implementation backed by Google's Perch v2 model.

    Loads the model once via birdnet.load_perch_v2() and reuses it across
    campaigns and files. The lib handles audio I/O, resampling to 32 kHz,
    5 s window framing, and batched TF inference. We operate in raw-logit
    space (apply_sigmoid=False) and translate to a calibrated probability
    only at the CSV boundary; see _PERCH_LOGIT_OFFSET for why a vanilla
    sigmoid is not appropriate for Perch v2. Output goes to
    <campaign>/<campaign>-detections-Perch-2.0.csv so it can coexist with a
    parallel BirdNET run on the same campaign.

    Performance (Apple M4 Pro, CPU only, 4 h 3 min of audio, 243 WAV files):
        Perch v2:     ~3 min 25 s wall, ~77x real-time   (RTF ~0.013)
        BirdNET v2.4: ~15 s wall,     ~1050x real-time (RTF ~0.001)
    Perch is roughly 13x slower per second of audio than BirdNET. The
    gap is the cost of Perch's larger conformer-style architecture vs
    BirdNET's small CNN; on GPU the gap narrows considerably. Plan for
    roughly 50 s of wall time per 1 h of audio on this hardware, and
    set user-facing ETAs accordingly.
    """

    model_key = "Perch-2.0"

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

        # Normalize the preferred-lang code in case the project was saved
        # under the old short-code scheme ('en' -> 'en_us').
        preferred_lang = normalize_lang_code(preferred_lang)

        model = birdnet.load_perch_v2(device="CPU")

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
                logging.exception("perch: campaign %s failed: %s", ci.name, exc)
                raise
            files_completed += ci_total

        return AnalysisRunResult(
            campaigns=tuple(results),
            elapsed=time.monotonic() - t0,
        )
