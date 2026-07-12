"""Base class for AnalysisRunner implementations.

BaseAnalysisRunner owns the per-campaign sequencing, progress reporting,
species-filter resolution, per-week species-list TXT writing, ARU and rank
computation, and CSV writing. Subclasses (BirdnetRunner, PerchRunner) fill
in the per-model bits via three abstract methods: _load_model,
_open_predict_session, and _parse_row.

Lifecycle of one run() call:

    _load_model() once
    for each campaign:
        _run_campaign()
            emit 'preparing'
            resolve species filter (shared)
            write per-week species-list TXT (shared)
            emit 'analyzing'
            _open_predict_session() per campaign (subclass picks kwargs)
                session.run(files)
            emit 'parsing'
            for each raw lib row:
                _parse_row() per row (subclass interprets the row)
                shared: rank, ARU, file_rel, week, CSV write
            emit 'done'
"""

from __future__ import annotations

import csv
import logging
import shutil
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from ..domain import (
    AnalysisProgress,
    AnalysisRunResult,
    AnalysisSettings,
    CampaignRunInput,
    CancelledError,
    Detection,
)
from ..domain import detection_schema as schema
from ..domain.audio_import import WEEK_YEAR_ROUND
from ..domain.entities import CampaignRunResult
from . import paths
from ._analysis_helpers import (
    RunGlobalProgress,
    build_allowed_lookup,
    build_progress_callback,
    count_audio_files,
    emit_progress,
    list_audio_files,
    parse_recording_time,
    week_from_path,
    write_species_list_files,
)
from .birdnet_lib import available_locales as _available_locales
from .birdnet_lib import locale_label_map, normalize_lang_code


@dataclass(frozen=True)
class ParsedRow:
    """Model-agnostic view of one raw library result row.

    Subclasses translate a raw structured-array row from the lib (which
    differs in confidence units and species-name encoding by model) into
    one of these for the base class to write to CSV.
    """

    file_path: Path
    start_time: float
    end_time: float
    scientific_name: str
    confidence: float  # probability 0-1, after any per-model calibration
    preferred_common: str  # for the "Species" CSV column
    # Keyed by locale code without the "Species_" prefix (e.g. "en_us").
    locale_commons: dict[str, str]


class BaseAnalysisRunner(ABC):
    """Shared scaffold for AnalysisRunner implementations.

    The public interface (run, count_audio_files, available_locales)
    matches what AnalysisRunner callers expect; concrete subclasses provide
    model-specific behaviour via _load_model, _open_predict_session, and
    _parse_row.
    """

    model_key: ClassVar[str]
    log_prefix: ClassVar[str]

    def count_audio_files(self, campaign_dir: Path) -> int:
        return count_audio_files(campaign_dir)

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
        output_base.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()

        # Project files saved under the old birdnet_analyzer locale scheme
        # used short codes ('en', 'de'); the new lib uses 'en_us' / 'en_uk'
        # / 'de'. Normalise so a stale 'en' degrades to 'en_us' silently.
        preferred_lang = normalize_lang_code(preferred_lang)

        logging.info("%s: loading model...", self.log_prefix)
        model = self._load_model()
        logging.info("%s: model loaded.", self.log_prefix)

        per_campaign_totals = [count_audio_files(ci.folder) for ci in campaigns]
        run_total = sum(per_campaign_totals)
        run_progress = RunGlobalProgress(progress, run_total)

        results: list[CampaignRunResult] = []
        total = len(campaigns)
        files_completed = 0
        for i, (ci, ci_total) in enumerate(
            zip(campaigns, per_campaign_totals, strict=True), start=1
        ):
            if progress.is_cancelled():
                raise CancelledError()
            run_progress.start_campaign(files_completed)
            camp_out = output_base / ci.name
            try:
                results.append(
                    self._run_campaign(
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
                logging.exception(
                    "%s: campaign %s failed: %s", self.log_prefix, ci.name, exc
                )
                raise
            files_completed += ci_total

        return AnalysisRunResult(
            campaigns=tuple(results),
            elapsed=time.monotonic() - t0,
        )

    def _run_campaign(
        self,
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

        emit_progress(
            progress,
            campaign=campaign_name,
            campaign_index=campaign_index,
            total_campaigns=total_campaigns,
            files_done=0,
            files_total=0,
            phase="preparing",
        )

        wav_files = list_audio_files(ci.folder)
        wav_count = len(wav_files)

        detections_csv = paths.campaign_csv_for_model(
            output_dir.parent, campaign_name, self.model_key
        )

        # Resolve the species filter before opening the inference session:
        # in LOCATION mode this pre-warms the geo model and computes per-
        # week whitelists, so any geo lookup cost is paid during 'preparing'.
        allowed_for, lat, lon, per_week_allowed, must_haves = build_allowed_lookup(
            ci, wav_files
        )

        # Write the applied per-week allow-list (geo + must-haves) alongside
        # the detections so the user can inspect exactly what the model was
        # asked to consider. Must-have entries are tagged with a
        # `# must-have` marker; the parser ignores comments so the file
        # round-trips cleanly if anyone pastes lines back into a campaign's
        # species_list.txt.
        species_list_txt = write_species_list_files(
            output_dir, campaign_name, per_week_allowed, must_haves
        )

        fieldnames = schema.write_fieldnames(settings.locales)

        if wav_count == 0:
            with open(detections_csv, "w", newline="", encoding="utf-8") as outfile:
                csv.DictWriter(outfile, fieldnames=fieldnames).writeheader()
            emit_progress(
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
                model_key=self.model_key,
            )

        emit_progress(
            progress,
            campaign=campaign_name,
            campaign_index=campaign_index,
            total_campaigns=total_campaigns,
            files_done=0,
            files_total=wav_count,
            phase="analyzing",
        )

        # session_ref is a one-slot list so the progress callback (built
        # before the session exists) can reach the session via closure once
        # we bind it inside the `with` block below.
        session_ref: list[Any] = [None]
        on_stats = build_progress_callback(
            progress,
            campaign=campaign_name,
            campaign_index=campaign_index,
            total_campaigns=total_campaigns,
            files_total=wav_count,
            session_ref=session_ref,
        )

        logging.info(
            "%s: opening predict session for campaign %s (%d files)...",
            self.log_prefix,
            campaign_name,
            wav_count,
        )
        with self._open_predict_session(
            model,
            settings=settings,
            files_total=wav_count,
            on_stats=on_stats,
        ) as session:
            session_ref[0] = session
            birdnet_log = self._birdnet_session_log_path(session)
            if birdnet_log is not None:
                logging.info(
                    "%s: birdnet internal session log: %s", self.log_prefix, birdnet_log
                )
            logging.info(
                "%s: session.run() starting for campaign %s...",
                self.log_prefix,
                campaign_name,
            )
            try:
                result = session.run(wav_files)
            except Exception as exc:
                logging.info(
                    "%s: session.run() raised for campaign %s: %s",
                    self.log_prefix,
                    campaign_name,
                    exc,
                )
                # Copy birdnet's internal log now: the library only copies it
                # into place on a clean session exit (session.__exit__ ->
                # ProcessManager.join()), and that join can itself hang if a
                # worker/producer process never exits, so this may be the
                # only copy that ever lands.
                self._save_birdnet_session_log(birdnet_log, campaign_name)
                if isinstance(exc, RuntimeError) and progress.is_cancelled():
                    raise CancelledError() from exc
                raise
            logging.info(
                "%s: session.run() finished for campaign %s.",
                self.log_prefix,
                campaign_name,
            )
        logging.info(
            "%s: predict session closed for campaign %s.",
            self.log_prefix,
            campaign_name,
        )

        if progress.is_cancelled():
            raise CancelledError()

        emit_progress(
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

            # Rank is recomputed per (file, chunk_start) over rows that
            # survive the per-week allow-list. The lib already sorts rows by
            # (file_idx asc, chunk_idx asc, confidence desc); dropping
            # out-of-region rows preserves that order, so a streaming pass
            # that resets on key change yields the right rank without
            # re-sorting.
            prev_key: tuple[str, float] | None = None
            rank = 0
            for raw_row in arr:
                parsed = self._parse_row(
                    raw_row,
                    preferred_lang_map=preferred_lang_map,
                    locale_maps=locale_maps,
                    settings=settings,
                )

                allowed = allowed_for(parsed.file_path)
                if allowed is not None and parsed.scientific_name not in allowed:
                    filtered_count += 1
                    continue

                try:
                    aru = parsed.file_path.relative_to(ci.folder).parts[0]
                except (ValueError, IndexError):
                    aru = ""
                aru_set.add(aru)

                try:
                    file_rel = parsed.file_path.relative_to(audio_root).as_posix()
                except ValueError:
                    file_rel = parsed.file_path.as_posix()

                recording_time = parse_recording_time(parsed.file_path.stem)
                file_week = week_from_path(parsed.file_path)

                key = (str(parsed.file_path), parsed.start_time)
                if key != prev_key:
                    prev_key = key
                    rank = 1
                else:
                    rank += 1

                # Serialize through the schema's Detection path so this
                # writer cannot drift from what the repo and table read.
                # Rounding mirrors the precision of the old formatting
                # (%.1f times, %.4f confidence) and coerces numpy scalars
                # from the lib into plain floats.
                detection = Detection(
                    campaign=campaign_name,
                    aru=aru,
                    week=file_week if file_week is not None else WEEK_YEAR_ROUND,
                    species=parsed.preferred_common,
                    scientific_name=parsed.scientific_name,
                    confidence=round(float(parsed.confidence), 4),
                    start_time=round(float(parsed.start_time), 1),
                    end_time=round(float(parsed.end_time), 1),
                    rank=rank,
                    file=file_rel,
                    recording_time=str(recording_time) if recording_time else "",
                    lat=lat,
                    lon=lon,
                    min_conf=settings.min_conf,
                    model=self.model_key,
                    extra={
                        schema.locale_column(loc): parsed.locale_commons.get(loc, "")
                        for loc in settings.locales
                    },
                )
                writer.writerow(schema.detection_to_row(detection))
                detection_count += 1

        if filtered_count:
            logging.info(
                "%s: per-week species filter dropped %d row(s); %d kept",
                self.log_prefix,
                filtered_count,
                detection_count,
            )

        emit_progress(
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
            model_key=self.model_key,
        )

    def _birdnet_session_log_path(self, session: Any) -> Path | None:
        """Look up birdnet's own per-session log file, if the lib exposes one.

        Reaches into the lib's private `_resources` attribute since this path
        isn't part of its public API; any failure here is swallowed so a lib
        internals change can't break an analysis run.
        """
        try:
            return session._resources.logging_resources.session_log_file
        except AttributeError:
            return None

    def _save_birdnet_session_log(self, birdnet_log: Path | None, campaign_name: str) -> None:
        """Copy birdnet's session log next to ours as soon as an error surfaces.

        birdnet only copies this file into place on a clean session exit
        (session.__exit__ -> ProcessManager.join()); if a worker/producer
        process never exits, that join can hang too, so this may be the only
        copy that ever lands.
        """
        if birdnet_log is None or not birdnet_log.exists():
            return
        dest = paths.log_dir() / f"birdnet-session-{self.log_prefix}-{campaign_name}-crash.log"
        try:
            shutil.copyfile(birdnet_log, dest)
            logging.info("%s: copied birdnet session log to %s", self.log_prefix, dest)
        except Exception as exc:  # noqa: BLE001  best-effort: never shadow the real error
            logging.warning("%s: failed to copy birdnet session log: %s", self.log_prefix, exc)

    # ---- Subclass hooks ----------------------------------------------------

    @abstractmethod
    def _load_model(self) -> Any:
        """Load the model once at the start of run().

        Called once per run(). The returned object must support
        predict_session(...) in a way that _open_predict_session can use.
        """

    @abstractmethod
    def _open_predict_session(
        self,
        model: Any,
        *,
        settings: AnalysisSettings,
        files_total: int,
        on_stats: Callable[[Any], None],
    ) -> AbstractContextManager[Any]:
        """Open the inference session as a context manager.

        Subclasses translate `settings.min_conf` and `settings.overlap` into
        the lib's predict_session kwargs. BirdNET works in probability space
        with apply_sigmoid=True; Perch works in raw-logit space with
        apply_sigmoid=False and a top-k cap.
        """

    @abstractmethod
    def _parse_row(
        self,
        raw_row: Any,
        *,
        preferred_lang_map: dict[str, str],
        locale_maps: dict[str, dict[str, str]],
        settings: AnalysisSettings,
    ) -> ParsedRow:
        """Convert one raw lib result row into a ParsedRow.

        The lib's row layout differs by model: BirdNET emits 'Sci_Common' in
        species_name and a sigmoid probability in confidence; Perch emits a
        raw Latin name and a raw logit. Subclasses normalise both into a
        ParsedRow with probability-space confidence.
        """
