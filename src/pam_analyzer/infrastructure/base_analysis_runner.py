"""Base class for AnalysisRunner implementations.

BaseAnalysisRunner owns the per-campaign sequencing, progress reporting,
species-filter resolution, per-week species-list TXT writing, ARU and rank
computation, and CSV writing. Subclasses fill in the per-model bits via
three abstract methods (_load_model, _open_predict_session, _parse_row)
plus a `taxonomy` binding that supplies their model's species axis, geo
filter and locale set.

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
import math
import shutil
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from ..domain import (
    AnalysisProgress,
    AnalysisSettings,
    Campaign,
    CancelledError,
    Detection,
    paths,
    week_from_path,
)
from ..domain import detection_schema as schema
from ..domain.analysis_run_result import AnalysisRunResult, CampaignRunResult, RunStatus
from ..domain.audio_import import WEEK_YEAR_ROUND, parse_recording_time
from ._analysis_helpers import (
    RunGlobalProgress,
    build_progress_callback,
    count_audio_files,
    emit_progress,
    list_audio_files,
    write_species_list_files,
)
from .birdnet_lib import TaxonomyServices, normalize_lang_code
from .legacy_names import expand_species


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
    scientific_name: str  # The name written to CSV, on the project's canonical_taxonomy axis
    confidence: float  # probability 0-1, after any per-model calibration
    preferred_common: str  # for the "Species" CSV column
    locale_commons: dict[str, str]  # Keyed by locale code without the "Species_" prefix (e.g. "en_us")
    # The name the species filter matches on, on the running model's own
    # axis: what the model emitted before to_axis() rewrote scientific_name
    # onto the project's axis. Both runners set it, since either can be the
    # one being rewritten depending on which axis the project chose. None
    # falls back to scientific_name, for a runner that never rewrites.
    match_name: str | None = None


class BaseAnalysisRunner(ABC):
    """Shared scaffold for AnalysisRunner implementations.

    The public interface (run, count_audio_files, available_locales)
    matches what AnalysisRunner callers expect. Concrete subclasses provide
    model-specific behaviour via _load_model, _open_predict_session, and
    _parse_row.
    """

    model_key: ClassVar[str]
    log_prefix: ClassVar[str]
    taxonomy: ClassVar[TaxonomyServices]  # The label axis, geo filter and locale set of the model this runner loads

    def count_audio_files(self, campaign_dir: Path) -> int:
        return count_audio_files(campaign_dir)

    def available_locales(self) -> list[str]:
        return list(self.taxonomy.available_locales())

    def run(
        self,
        *,
        campaigns: list[Campaign],
        settings: AnalysisSettings,
        preferred_lang: str,
        progress: AnalysisProgress,
    ) -> AnalysisRunResult:
        t0 = time.monotonic()

        # Project files saved under the old birdnet_analyzer locale scheme
        # used short codes ('en', 'de'). The new lib uses 'en_us' / 'en_uk'
        # / 'de'. Normalise so a stale 'en' degrades to 'en_us' silently.
        preferred_lang = normalize_lang_code(preferred_lang)

        logging.info("%s: loading model...", self.log_prefix)
        model = self._load_model()
        logging.info("%s: model loaded.", self.log_prefix)

        per_campaign_totals = [count_audio_files(c.folder) for c in campaigns]
        run_total = sum(per_campaign_totals)
        run_progress = RunGlobalProgress(progress, run_total)

        # Each finished campaign has already written its CSV to disk, so a
        # cancel or a mid-batch failure must not throw the completed ones
        # away. Instead of raising past `results`, the loop stops and the
        # accumulated campaigns are returned with the outcome that ended it.
        results: list[CampaignRunResult] = []
        total = len(campaigns)
        files_completed = 0
        status = RunStatus.COMPLETED
        error: str | None = None
        for i, (campaign, campaign_total) in enumerate(
            zip(campaigns, per_campaign_totals, strict=True), start=1
        ):
            if progress.is_cancelled():
                status = RunStatus.CANCELLED
                break
            run_progress.start_campaign(files_completed)
            try:
                results.append(
                    self._run_campaign(
                        campaign,
                        settings,
                        preferred_lang,
                        run_progress,
                        i,
                        total,
                        model,
                    )
                )
            except CancelledError:
                status = RunStatus.CANCELLED
                break
            except Exception as exc:  # noqa: BLE001
                logging.exception(
                    "%s: campaign %s failed: %s", self.log_prefix, campaign.name, exc
                )
                status = RunStatus.FAILED
                error = str(exc)
                break
            files_completed += campaign_total

        return AnalysisRunResult(
            status=status,
            campaigns=tuple(results),
            elapsed=time.monotonic() - t0,
            error=error,
        )

    def _run_campaign(
        self,
        campaign: Campaign,
        settings: AnalysisSettings,
        preferred_lang: str,
        progress: AnalysisProgress,
        campaign_index: int,
        total_campaigns: int,
        model: Any,
    ) -> CampaignRunResult:
        campaign_name = campaign.name
        t0 = time.monotonic()
        # Analysis artifacts live inside the campaign folder itself, so a
        # campaign stays self-contained and relocatable.
        output_dir = campaign.folder

        emit_progress(
            progress,
            campaign=campaign_name,
            campaign_index=campaign_index,
            total_campaigns=total_campaigns,
            files_done=0,
            files_total=0,
            phase="preparing",
        )

        wav_files = list_audio_files(campaign.folder)
        wav_count = len(wav_files)

        detections_csv = schema.campaign_csv_for_model(campaign.folder, self.model_key)

        # Resolve the species filter before opening the inference session.
        # In LOCATION mode this pre-warms the geo model and computes per-week allowlists,
        # so any geo lookup cost is paid during 'preparing'.
        resolved = campaign.load_species_filter().resolve(
            wav_files,
            self.taxonomy.region_species_scientific,
            resolve_names=self._resolve_list_names,
        )
        lat = resolved.location.latitude if resolved.location else None
        lon = resolved.location.longitude if resolved.location else None

        # Write the applied per-week allowlist (geo + must-haves) alongside
        # the detections so the user can inspect exactly what the model was
        # asked to consider. Must-have entries are tagged with a `# must-have`
        # marker. The parser ignores comments so the file round-trips cleanly
        # if anyone pastes lines back into a campaign's species_list.txt. The
        # written path is rediscovered on demand (AnalysisInventoryEntry), so
        # the run result does not need to carry it.
        write_species_list_files(
            output_dir, resolved.per_week_allowed, resolved.must_haves
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
                detections_csv=detections_csv,
                detection_count=0,
                wav_count=0,
                aru_count=0,
                elapsed=time.monotonic() - t0,
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
                # Copy birdnet's log into our log dir as the lib keeps it
                # in the temp dir and only renames it at the very end of
                # session.__exit__, and teardown that raises or blocks skips
                # that.
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

        preferred_lang_map = self.taxonomy.locale_label_map(preferred_lang)
        locale_maps = {loc: self.taxonomy.locale_label_map(loc) for loc in settings.locales}

        detection_count = 0
        # Two reasons a row can be dropped by the per-week allow-list, tracked
        # apart so a legacy-name mismatch does not hide behind ordinary geography.
        out_of_region_count = 0  # a known bird, just not expected here
        unknown_species_count = 0  # name absent from the model's axis entirely
        nonfinite_count = 0  # see the guard in the row loop below
        aru_set: set[str] = set()

        arr = result.to_structured_array()

        # Write to a sibling temp file and swap it into place only once every
        # row is on disk (Path.replace is atomic within a folder)
        tmp_csv = detections_csv.with_name(detections_csv.name + ".tmp")

        with open(tmp_csv, "w", newline="", encoding="utf-8") as outfile:
            writer = csv.DictWriter(outfile, fieldnames=fieldnames)
            writer.writeheader()

            # Rank is recomputed per (file, chunk_start) over rows that
            # survive the per-week allow-list. The lib already sorts rows by
            # (file_idx asc, chunk_idx asc, confidence desc). Dropping
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

                # A model can hand back a non-finite score for degenerate
                # input. BirdNET v3.0 normalizes each window by its own
                # standard deviation, so a digitally constant window (a dead
                # ARU channel, a dropout, a muted track) divides by zero and
                # every class comes back NaN. Dropping those rows keeps one
                # bad file from aborting the whole campaign when the CSV
                # writer tries to format the value.
                if not math.isfinite(parsed.confidence):
                    nonfinite_count += 1
                    continue

                # Match on the model's own axis as the allow-list came from the
                # geo model of the same generation, so both sides speak the
                # same taxonomy even when scientific_name has been rewritten
                # onto the project's axis for output.
                match_name = parsed.match_name or parsed.scientific_name
                allowed = resolved.allowed_for(parsed.file_path)
                if allowed is not None and match_name not in allowed:
                    if match_name in self._known_output_species():
                        out_of_region_count += 1
                    else:
                        unknown_species_count += 1
                    continue

                try:
                    aru = parsed.file_path.relative_to(campaign.folder).parts[0]
                except (ValueError, IndexError):
                    aru = ""
                aru_set.add(aru)

                # Campaign-relative so the campaign folder can be renamed or
                # moved without invalidating its own CSV.
                try:
                    file_rel = parsed.file_path.relative_to(campaign.folder).as_posix()
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

        # Every row is now flushed and the file handle closed, so the swap
        # publishes a complete CSV under the final name in one atomic step.
        tmp_csv.replace(detections_csv)

        if nonfinite_count:
            logging.warning(
                "%s: dropped %d row(s) with a non-finite confidence. Some audio has windows of constant amplitude",
                self.log_prefix,
                nonfinite_count,
            )

        if out_of_region_count or unknown_species_count:
            logging.info(
                "%s: per-week species filter dropped %d row(s): %d out-of-region, "
                "%d not on the model's axis (legacy name or non-bird). %d kept",
                self.log_prefix,
                out_of_region_count + unknown_species_count,
                out_of_region_count,
                unknown_species_count,
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
            detections_csv=detections_csv,
            detection_count=detection_count,
            wav_count=wav_count,
            aru_count=len(aru_set),
            elapsed=time.monotonic() - t0,
        )

    def _birdnet_session_log_path(self, session: Any) -> Path | None:
        """Look up birdnet's own per-session log file, if the lib exposes one.

        Reaches into the lib's private `_resources` attribute since this path
        isn't part of its public API. Any failure here is swallowed so a lib
        internals change can't break an analysis run.
        """
        try:
            return session._resources.logging_resources.session_log_file
        except AttributeError:
            return None

    def _save_birdnet_session_log(self, birdnet_log: Path | None, campaign_name: str) -> None:
        """Copy birdnet's session log next to ours as soon as an error surfaces.

        birdnet writes this file live into the temp dir and only copies it to
        its global temp name at the end of session.__exit__, so an error leaves
        the evidence in a temp file under a session-hash name. This puts a
        campaign-tagged copy in our log dir instead.
        """
        if birdnet_log is None or not birdnet_log.exists():
            return
        dest = paths.log_dir() / f"birdnet-session-{self.log_prefix}-{campaign_name}-crash.log"
        try:
            shutil.copyfile(birdnet_log, dest)
            logging.info("%s: copied birdnet session log to %s", self.log_prefix, dest)
        except Exception as exc:  # noqa: BLE001  best-effort: never shadow the real error
            logging.warning("%s: failed to copy birdnet session log: %s", self.log_prefix, exc)

    def _known_output_species(self) -> AbstractSet[str]:
        """Every scientific name this runner's model can emit.

        Separate from taxonomy.known_species_scientific() because a runner may
        borrow another generation's taxonomy for geo lookups and locale labels
        while emitting its own, wider set of classes. Overridden where that is
        the case, and the only per-engine fact the shared pipeline needs
        beyond the three abstract methods: it classifies a filter drop and it
        settles how a species-list line is read.
        """
        return self.taxonomy.known_species_scientific()

    def _resolve_list_names(self, lines: frozenset[str]) -> frozenset[str]:
        """The domain's ResolveNames port for this runner.

        Reads each line the way this engine spells a list entry, then adds the
        other taxonomy's spelling of each name so a list written for one
        BirdNET generation still matches the other. A spelling the running
        model does not emit simply never matches.
        """
        return expand_species(frozenset(self._read_list_entry(line) for line in lines))

    def _read_list_entry(self, line: str) -> str:
        """Read one line of a user's species list as a name this model emits.

        A line that already names one of the model's classes is that class,
        which is what keeps Perch's underscored sound events ('Acoustic_guitar')
        whole. Anything else came from a BirdNET species list, which spells one
        entry 'Scientific_Common', and loses its common-name half. That is also
        the right reading for a typo.

        One rule for every engine rather than one per engine: a line without an
        underscore splits to itself, so the two branches can only disagree
        about an underscored line, which is exactly what the axis settles.
        """
        return line if line in self._known_output_species() else line.split("_", 1)[0].strip()

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
        the lib's predict_session kwargs, along with whatever scoring options
        the model needs (sigmoid handling, top-k caps, device selection).
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

        The lib's row layout differs by model, both in how species_name
        encodes the name and in whether confidence arrives as a probability
        or a raw logit. Subclasses normalise theirs into a ParsedRow whose
        confidence is in probability space and whose scientific_name is on
        the axis the species filter checks against.
        """
