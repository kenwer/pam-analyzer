"""Shared scaffolding for the BirdNET and Perch v2 analysis runners.

These helpers are model-agnostic: file discovery, recording-time parsing,
species-list resolution, progress translation, and the per-week species-list
TXT writer. Both runners share this module via the BaseAnalysisRunner class
and via direct imports of the leaf utilities.

Module-level functions (rather than methods on the base class) by design:
nothing here touches per-run state, and keeping them as plain functions
prevents override-by-accident from a subclass and keeps them callable from
non-runner code (e.g. counting files before a run starts).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..domain import (
    AnalysisProgress,
    AnalysisProgressSnapshot,
    CampaignRunInput,
    FilterMode,
)
from ..domain.audio_import import WEEK_YEAR_ROUND
from . import paths
from .birdnet_lib import region_species_scientific


def count_audio_files(campaign_dir: Path) -> int:
    return sum(
        1 for f in campaign_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in paths.AUDIO_EXTENSIONS
    )


def list_audio_files(campaign_dir: Path) -> list[Path]:
    return sorted(
        f for f in campaign_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in paths.AUDIO_EXTENSIONS
    )


def week_from_path(path: Path) -> int | None:
    """Extract the ISO week number from a 'week_NN' path segment, or None."""
    for part in path.parts:
        if part.startswith("week_"):
            try:
                return int(part.split("_", 1)[1])
            except (IndexError, ValueError):
                pass
    return None


def parse_species_lines(text: str) -> frozenset[str]:
    """Parse a user-supplied species blob into a set of scientific names.

    Accepts plain Latin names or 'Scientific_Common' entries copied from a
    BirdNET-style species list, so users can paste either format without
    converting. Everything after a `#` on a line is treated as a comment, so
    users can annotate their lists or paste back lines a runner emitted with
    `  # must-have` markers.
    """
    out: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        out.add(line.split("_", 1)[0].strip())
    return frozenset(out)


def emit_progress(
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


class RunGlobalProgress:
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


def build_allowed_lookup(
    ci: CampaignRunInput, wav_files: list[Path]
) -> tuple[
    Callable[[Path], frozenset[str] | None],
    float | None,
    float | None,
    dict[int, frozenset[str]],
    frozenset[str],
]:
    """Per-file species-allow-list resolver and the (lat, lon) for the run.

    Returns a callable `allowed_for(path)` that the row loop calls once per
    detection to decide whether to keep it, plus the per-week applied set
    (geo + must-haves) and the must-have subset, both used by the
    species-list TXT writer. Three regimes:

    - GLOBAL or empty input: callable always returns None, meaning "no
      filter; keep every row". The caller short-circuits the check.
    - LIST with `species_list_text`: callable returns a single fixed
      frozenset for any path. Same allow-list applies to every week.
    - LOCATION with lat/lon: callable looks up the file's week from its
      path and returns the precomputed regional set for that week.
      Must-have species are unioned on top so a user-added bird never gets
      filtered out even if the geo model considers it implausible.

    The per-week sets are computed eagerly from the wav file list so any geo
    downloads happen during the 'preparing' phase rather than mid-inference.
    """
    if ci.mode == FilterMode.LIST and ci.species_list_text:
        fixed = parse_species_lines(ci.species_list_text)
        return (lambda _p: fixed), None, None, {}, frozenset()

    if ci.mode == FilterMode.LOCATION and ci.location is not None:
        lat = ci.location.latitude
        lon = ci.location.longitude
        must_haves = parse_species_lines(ci.must_have_species_text or "")
        weeks_present: set[int] = set()
        for f in wav_files:
            w = week_from_path(f)
            weeks_present.add(w if w is not None else WEEK_YEAR_ROUND)
        # WEEK_YEAR_ROUND is passed straight to the geo model, whose API
        # defines -1 as "predict the year-round species list".
        per_week: dict[int, frozenset[str]] = {
            w: region_species_scientific(lat, lon, w) | must_haves
            for w in weeks_present
        }

        def lookup(path: Path) -> frozenset[str] | None:
            w = week_from_path(path)
            return per_week.get(w if w is not None else WEEK_YEAR_ROUND)

        return lookup, lat, lon, per_week, must_haves

    return (lambda _p: None), None, None, {}, frozenset()


def write_species_list_files(
    output_dir: Path,
    per_week_allowed: dict[int, frozenset[str]],
    must_haves: frozenset[str],
) -> Path | None:
    """Write the per-week applied species list as plain text.

    Each file contains the merged list (geo + must-haves) the runner
    actually filtered against, with `  # must-have` appended to lines whose
    species came from the user's must-have input. One file per week when
    week_NN folders are present, or a single applied-species-list.txt
    when they are not; that single-file path is returned for inclusion in
    CampaignRunResult.species_list_txt. Filenames carry no campaign name so
    a campaign folder rename never orphans them, and the 'applied-' prefix
    keeps them apart from the user inputs species_list.txt and
    must_have_species.txt.
    """
    if not per_week_allowed:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    weeks = sorted(per_week_allowed)
    if weeks == [WEEK_YEAR_ROUND]:
        single_path = paths.applied_species_list_file(output_dir)
        single_path.write_text(
            _format_species_lines(per_week_allowed[WEEK_YEAR_ROUND], must_haves),
            encoding="utf-8",
        )
        return single_path

    for w, species in per_week_allowed.items():
        if w == WEEK_YEAR_ROUND:
            # Files without a week_NN segment in a campaign that does have
            # week folders are rare; fall back to a 'no-week' file so the
            # list is still preserved.
            paths.applied_species_list_file(output_dir).write_text(
                _format_species_lines(species, must_haves), encoding="utf-8"
            )
        else:
            (output_dir / f"applied-species-list-week-{w:02d}.txt").write_text(
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


def build_progress_callback(
    progress: AnalysisProgress,
    *,
    campaign: str,
    campaign_index: int,
    total_campaigns: int,
    files_total: int,
    session_ref: list[Any],
) -> Callable[[Any], None]:
    """Bridge AcousticProgressStats from the lib to our snapshot port.

    Approximates files_done from `stats.progress_pct` because the lib
    reports progress in segments processed, not files. The lib's own
    estimated time remaining is forwarded verbatim as `phase_detail` so the
    UI can render an ETA in place of the per-file path we dropped when
    moving to single-session inference.

    The callback also serves as our cancellation hook: when the user clicks
    Stop, is_cancelled() flips True and we tell the lib's session to wind
    down, which makes session.run raise RuntimeError on exit.
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
        emit_progress(
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
