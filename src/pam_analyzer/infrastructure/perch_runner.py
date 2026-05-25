"""Perch v2 infrastructure adapter (sketch, not yet wired into the UI).

PerchRunner is an alternative AnalysisRunner implementation that uses
Google's Perch v2 SavedModel instead of BirdNET. It exists alongside
BirdnetRunner, not as a branch inside it, because the two models differ
in ways that would otherwise litter birdnet_runner.py with conditionals:

  - Perch is one TensorFlow SavedModel loaded once in-process. BirdNET
    runs as a TFLite model inside a multiprocessing.Pool of workers.
  - Perch's classifier head is a single global softmax over eBird six
    letter codes. There is no per-week or per-region species filter.
  - Perch wants 5 s windows at 32 kHz. BirdNET wants 3 s at 48 kHz.

Sequencing per campaign mirrors BirdnetRunner so the UI progress code
does not need to know which runner is active:

    PerchRunner.run(...)
      -> _ensure_model() once at the top of the run
      -> _run_campaign(...) per campaign
           -> _emit_progress("preparing")
           -> _emit_progress("analyzing")
           -> per audio file:
                read, resample to 32 kHz, window into 5 s segments,
                batch through saved_model.signatures["serving_default"],
                softmax, threshold by min_conf, append rows
                _emit_progress("analyzing", k/N)
           -> _emit_progress("parsing")  # row write happens inline
           -> _emit_progress("done")
"""

from __future__ import annotations

import csv
import logging
import re
import time
from dataclasses import replace
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
from .birdnet_runner import _get_available_locales, _load_locale_labels

if TYPE_CHECKING:
    import numpy as np

PERCH_SAMPLE_RATE = 32000
PERCH_WINDOW_SECONDS = 5.0
# Tunable. Larger = fewer Python/TF round trips but more memory.
PERCH_BATCH_SIZE = 8


def _count_audio_files(campaign_dir: Path) -> int:
    return sum(
        1 for f in campaign_dir.rglob("*")
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
    """Extract the ISO week number from a 'week_NN' path segment, or None.

    Location-mode campaigns are organised as <campaign>/<aru>/week_NN/...
    Duplicated from birdnet_runner._week_from_path rather than imported so
    the two infrastructure adapters stay independent.
    """
    for part in path.parts:
        if part.startswith("week_"):
            try:
                return int(part.split("_", 1)[1])
            except (IndexError, ValueError):
                pass
    return None


def _parse_species_lines(text: str) -> frozenset[str]:
    """Parse a user-supplied species blob into a set of scientific names.

    Accepts either plain Latin names ('Parus major') or BirdNET's
    'Scientific_Common' format ('Parus major_Great Tit'), so users can
    copy-paste a slist from a BirdNET run as the input to a Perch run
    without converting it.
    """
    return frozenset(
        line.split("_", 1)[0].strip()
        for line in text.splitlines()
        if line.strip()
    )


def _prime_birdnet_for_species_list() -> None:
    """Populate cfg.LABELS so birdnet's get_species_list can run standalone.

    birdnet_analyzer's explore() reads cfg.LABELS to label its model output,
    but only the analyze and species-list entry points populate it. We call
    get_species_list directly, so we replicate that one-time setup here.
    Idempotent.
    """
    import birdnet_analyzer.config as cfg
    from birdnet_analyzer.utils import read_lines

    if not cfg.LABELS:
        cfg.LABELS = read_lines(cfg.BIRDNET_LABELS_FILE)


@cache
def _region_species(lat: float, lon: float, week: int) -> frozenset[str]:
    """Scientific names BirdNET considers possible at (lat, lon, week).

    Used to filter Perch's open-world output to a regional whitelist. Perch
    has no built-in geographic filter; without one, North American birds and
    iNat amphibians sneak into runs over European data. We delegate to
    BirdNET's get_species_list because the project already depends on it
    and the threshold semantics match what birdnet_runner uses (sf_thresh
    0.03).

    BirdNET formats list entries as 'Scientific_Common'; we keep only the
    scientific half because Perch's class axis is scientific names. Results
    are cached because a Perch run hits the same (lat, lon, week) triplet
    repeatedly across files from one ARU and week folder.
    """
    from birdnet_analyzer.species.utils import get_species_list  # type: ignore[import]

    _prime_birdnet_for_species_list()
    items = get_species_list(lat, lon, week, threshold=0.03)
    return frozenset(s.split("_", 1)[0] for s in items)


_PERCH_REQUIRED_FILES = (
    "fingerprint.pb",
    "saved_model.pb",
    "variables/variables.index",
    "variables/variables.data-00000-of-00001",
    "assets/labels.csv",
    "assets/perch_v2_ebird_classes.csv",
)


def _ensure_perch_model() -> Path:
    """Locate the Perch v2 SavedModel, downloading into the package dir if needed.

    The model always lives at birdnet_analyzer's cfg.PERCH_V2_MODEL_PATH, the
    same canonical location BirdNET uses for its own checkpoints. Build
    artifacts ship the directory pre-populated (scripts/build.py runs the
    download before PyInstaller, and --collect-data birdnet_analyzer sweeps
    it up). Dev runs from source hit the download path on first use; from
    then on the cached copy in the venv's package dir is reused.

    We use kagglehub.model_download with an explicit output_dir so the
    files land straight in the package dir rather than via a copytree from
    ~/.cache/kagglehub. That keeps dev and frozen runs reading from the
    same path and avoids the transient double-storage you would get with
    birdnet_analyzer.utils.ensure_perch_exists (which copytrees from the
    kagglehub cache).
    """
    import birdnet_analyzer.config as cfg

    in_package = Path(cfg.PERCH_V2_MODEL_PATH)
    if all((in_package / f).exists() for f in _PERCH_REQUIRED_FILES):
        return in_package

    import kagglehub

    in_package.mkdir(parents=True, exist_ok=True)
    kagglehub.model_download(
        "google/bird-vocalization-classifier/tensorFlow2/perch_v2_cpu",
        output_dir=str(in_package),
    )
    return in_package


@cache
def _english_common_names() -> dict[str, str]:
    """sci -> English common name from BirdNET's base labels file.

    BirdNET treats English as the model's native output, so its locale
    directory has no 'en' file; the canonical English mapping lives in
    cfg.BIRDNET_LABELS_FILE in 'Scientific_Common' format. Perch reuses
    it as the English common-name source so the user sees real names in
    the Species column instead of Latin binomials.
    """
    import birdnet_analyzer.config as cfg

    mapping: dict[str, str] = {}
    with open(cfg.BIRDNET_LABELS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "_" in line:
                sci, common = line.split("_", 1)
                mapping[sci] = common
    return mapping


def _localized_common_names(locale: str) -> dict[str, str]:
    """sci -> common-name dict for any locale BirdNET ships.

    'en' is handled specially because BirdNET ships English inside its
    main label file rather than as a separate locale file. All other
    locale codes delegate to birdnet_runner's cached loader.
    """
    if locale == "en":
        return _english_common_names()
    return _load_locale_labels(locale)


def _load_label_axis(model_path: Path) -> list[str]:
    """Read the per-class scientific names. Row order matches the model axis.

    labels.csv is a single-column CSV with one row per output class. Common
    names are not shipped by Google; the Species column ends up being the
    Latin name unless an external sci-to-common mapping is added later. A
    parallel perch_v2_ebird_classes.csv exists with eBird codes, but we
    don't surface it yet (most rows are 'no_ebird_code' anyway).
    """
    sci_path = model_path / "assets" / "labels.csv"
    with open(sci_path, encoding="utf-8", newline="") as f:
        rdr = csv.reader(f)
        next(rdr, None)  # header
        return [row[0] if row else "" for row in rdr]


def _read_audio_mono_32k(file_path: Path) -> np.ndarray:
    """Read an audio file as mono float32 at 32 kHz.

    Uses soundfile (already a project dep) for I/O and scipy.signal.resample_poly
    for the resample. Stereo inputs are averaged to mono. We do not stream;
    PAM recordings are typically minutes long, which fits comfortably in RAM
    even with the 32 kHz upsample.
    """
    import soundfile as sf
    from scipy.signal import resample_poly

    data, sr = sf.read(str(file_path), dtype="float32", always_2d=False)
    if data.ndim == 2:
        data = data.mean(axis=1).astype("float32", copy=False)
    if sr != PERCH_SAMPLE_RATE:
        from math import gcd

        g = gcd(int(sr), PERCH_SAMPLE_RATE)
        data = resample_poly(data, PERCH_SAMPLE_RATE // g, sr // g).astype("float32", copy=False)
    return data


def _frame_into_windows(samples: np.ndarray, overlap_seconds: float = 0.0) -> np.ndarray:
    """Slice a 1-D signal into 5 s windows with optional overlap.

    Returns an (n_windows, win) float32 array. Final window is zero-padded
    when the input length is not an exact multiple of the stride. Overlap
    is clamped to [0, 4.9] s to match birdnet_analyzer's Perch upper bound
    (anything closer to the full window length would produce a stride
    near zero and explode window count for no analytical benefit).
    """
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    win = int(PERCH_SAMPLE_RATE * PERCH_WINDOW_SECONDS)
    overlap_seconds = max(0.0, min(overlap_seconds, PERCH_WINDOW_SECONDS - 0.1))
    stride = int(round((PERCH_WINDOW_SECONDS - overlap_seconds) * PERCH_SAMPLE_RATE))

    n = len(samples)
    if n == 0:
        return np.zeros((0, win), dtype="float32")
    if n < win:
        out = np.zeros((1, win), dtype="float32")
        out[0, :n] = samples
        return out
    n_windows = 1 + (n - win + stride - 1) // stride
    needed = (n_windows - 1) * stride + win
    if needed > n:
        samples = np.concatenate([samples, np.zeros(needed - n, dtype="float32")])
    # sliding_window_view is zero-copy; slicing with [::stride] picks the
    # subset of starts we want. ascontiguousarray gives TF a packed tensor.
    return np.ascontiguousarray(sliding_window_view(samples, win)[::stride])


def _predict_batches(
    model: Any,
    windows: np.ndarray,
    batch_size: int,
) -> np.ndarray:
    """Run the SavedModel over batches of windows, return per-window softmax.

    Output shape is (n_windows, n_classes). softmax is taken across the class
    axis because the SavedModel returns logits under the 'label' key, matching
    birdnet_analyzer/model.py:1107.
    """
    import numpy as np
    import tensorflow as tf

    parts: list[np.ndarray] = []
    for start in range(0, len(windows), batch_size):
        batch = tf.constant(windows[start : start + batch_size])
        out = model.signatures["serving_default"](inputs=batch)
        parts.append(tf.nn.softmax(out["label"], axis=-1).numpy())
    if not parts:
        return np.zeros((0, 0), dtype="float32")
    return np.concatenate(parts, axis=0)


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
    """Same shape as BirdnetRunner's adapter, duplicated to avoid a cross-import."""

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
    sci_axis: list[str],
) -> CampaignRunResult:
    """Analyze one campaign with the Perch v2 SavedModel.

    The detections CSV schema matches what BirdnetRunner writes, so downstream
    panels (examine, charts) need no changes. Filter mode and location are
    accepted but ignored, because Perch has no geographic species filter.
    """
    import numpy as np

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

    wav_files = sorted(
        f for f in ci.folder.rglob("*")
        if f.is_file() and f.suffix.lower() in paths.AUDIO_EXTENSIONS
    )
    wav_count = len(wav_files)

    # Filter resolution mirrors BirdnetRunner: LIST mode with a non-empty
    # species_list_text supersedes any geographic filter; LOCATION mode
    # builds a per-week regional whitelist, optionally unioned with
    # must-have species. With neither, Perch's full 14,795 class axis is
    # emitted (modulo the min_conf threshold).
    lat: float | None = None
    lon: float | None = None
    if ci.mode == FilterMode.LOCATION and ci.location is not None:
        lat = ci.location.latitude
        lon = ci.location.longitude

    list_set: frozenset[str] | None = None
    if ci.mode == FilterMode.LIST and ci.species_list_text:
        list_set = _parse_species_lines(ci.species_list_text)

    must_haves: frozenset[str] = frozenset()
    if lat is not None and ci.must_have_species_text:
        must_haves = _parse_species_lines(ci.must_have_species_text)

    detections_csv = output_dir / f"{campaign_name}-detections.csv"
    run_context = {
        "Lat": lat if lat is not None else "",
        "Lon": lon if lon is not None else "",
        "Species_List": "",
        "Min_Conf": settings.min_conf,
        "Model": "perch_v2_cpu",
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

    detection_count = 0
    filtered_count = 0  # classes above threshold but not in the region whitelist
    aru_set: set[str] = set()

    # Load locale dictionaries up front so per-row lookups are O(1) string
    # hits instead of file reads. Each map is sci -> localized common name;
    # missing keys fall back to the scientific name (or empty for non-default
    # locale columns) when written below.
    locale_maps = {loc: _localized_common_names(loc) for loc in settings.locales}
    preferred_lang_map = _localized_common_names(preferred_lang)

    # Effective window step after the user's overlap is applied. Stays at
    # PERCH_WINDOW_SECONDS when overlap is 0 (the default), and shrinks
    # linearly as overlap grows. Used for both framing and Start_Time math
    # below so the two never drift.
    overlap_clamped = max(0.0, min(settings.overlap, PERCH_WINDOW_SECONDS - 0.1))
    step_seconds = PERCH_WINDOW_SECONDS - overlap_clamped

    _emit_progress(
        progress,
        campaign=campaign_name,
        campaign_index=campaign_index,
        total_campaigns=total_campaigns,
        files_done=0,
        files_total=wav_count,
        phase="analyzing",
    )

    with open(detections_csv, "w", newline="", encoding="utf-8") as outfile:
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()

        for i, wav in enumerate(wav_files, start=1):
            if progress.is_cancelled():
                raise CancelledError()

            try:
                samples = _read_audio_mono_32k(wav)
                windows = _frame_into_windows(samples, overlap_seconds=overlap_clamped)
                probs = _predict_batches(model, windows, PERCH_BATCH_SIZE)
            except Exception as exc:  # noqa: BLE001
                logging.warning("perch: skipping %s: %s", wav, exc)
                _emit_progress(
                    progress,
                    campaign=campaign_name,
                    campaign_index=campaign_index,
                    total_campaigns=total_campaigns,
                    files_done=i,
                    files_total=wav_count,
                    phase="analyzing",
                    phase_detail=wav.name,
                )
                continue

            try:
                aru = wav.relative_to(ci.folder).parts[0]
            except (ValueError, IndexError):
                aru = ""
            aru_set.add(aru)

            try:
                file_rel = wav.relative_to(audio_root).as_posix()
            except ValueError:
                file_rel = wav.as_posix()

            recording_time = _parse_recording_time(wav.stem)
            file_week = _week_from_path(wav)

            # Region filter applies when the campaign has lat/lon. For files
            # outside a week_NN tree we fall back to week=-1, which makes
            # BirdNET return the annual whitelist for the location.
            allowed: frozenset[str] | None = None
            if list_set is not None:
                allowed = list_set
            elif lat is not None and lon is not None:
                region = _region_species(lat, lon, file_week if file_week is not None else -1)
                allowed = region | must_haves if must_haves else region

            for w_idx in range(probs.shape[0]):
                # argsort descending gives rank order over classes at this segment.
                # We only emit classes above the threshold to keep the CSV small.
                order = np.argsort(-probs[w_idx])
                rank = 0
                start_t = w_idx * step_seconds
                end_t = start_t + PERCH_WINDOW_SECONDS
                for cls_idx in order:
                    conf = float(probs[w_idx, cls_idx])
                    if conf < settings.min_conf:
                        break  # softmax is monotonically decreasing across argsort
                    sci_name = sci_axis[cls_idx] if cls_idx < len(sci_axis) else ""
                    if allowed is not None and sci_name not in allowed:
                        filtered_count += 1
                        continue  # skip out-of-region class, keep scanning
                    rank += 1
                    # Resolve common name via BirdNET's shipped sci->common
                    # tables. Falls back to the Latin name when no entry
                    # exists (e.g. iNat-only classes BirdNET never trained on).
                    common = preferred_lang_map.get(sci_name, sci_name)
                    locale_names = {
                        f"Species_{loc}": locale_maps[loc].get(sci_name, "")
                        for loc in settings.locales
                    }
                    writer.writerow({
                        "Campaign": campaign_name,
                        "ARU": aru,
                        "Start_Time": f"{start_t:.1f}",
                        "End_Time": f"{end_t:.1f}",
                        "Scientific_Name": sci_name,
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

            _emit_progress(
                progress,
                campaign=campaign_name,
                campaign_index=campaign_index,
                total_campaigns=total_campaigns,
                files_done=i,
                files_total=wav_count,
                phase="analyzing",
                phase_detail=wav.name,
            )

    if filtered_count and (list_set is not None or lat is not None):
        logging.info(
            "perch: species filter removed %d detections (%d kept)",
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
        species_list_txt=None,
        detection_count=detection_count,
        wav_count=wav_count,
        aru_count=len(aru_set),
        elapsed=time.monotonic() - t0,
    )


class PerchRunner:
    """AnalysisRunner implementation backed by Google's Perch v2 SavedModel.

    Loads the model once on the main thread and reuses it across campaigns
    and files. Inference is single-process, single-threaded at the Python
    level; TensorFlow handles intra-op parallelism on CPU.
    """

    def count_audio_files(self, campaign_dir: Path) -> int:
        return _count_audio_files(campaign_dir)

    def available_locales(self) -> list[str]:
        # Perch's class axis is scientific names, so any BirdNET locale file
        # (which is a plain sci-to-common lookup) is reusable as-is. 'en'
        # is added because BirdNET ships English inside its main label file
        # rather than as a separate locale.
        return ["en", *_get_available_locales()]

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
        import tensorflow as tf

        output_base.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()

        model_path = _ensure_perch_model()
        model = tf.saved_model.load(str(model_path))
        sci_axis = _load_label_axis(model_path)

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
                    sci_axis,
                )
            )
            files_completed += ci_total

        return AnalysisRunResult(
            campaigns=tuple(results),
            elapsed=time.monotonic() - t0,
        )
