#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = ["pam-analyzer"]
#
# # The app from this checkout, not PyPI, so the script runs the current import code.
# [tool.uv.sources]
# pam-analyzer = { path = "..", editable = true }
# ///
"""Bring an older project's audio into the layout a current import produces.

Older PAM Analyzer versions copied WAV files from the SD card unchanged and
did not always sort them into week folders. Today the importer transcodes WAV
to 16-bit FLAC, verifies the round trip bit for bit, embeds the GUANO metadata
as a Vorbis comment and files each recording under <ARU>/week_NN by its
recording time. This script does both for every campaign of a project:

1. Convert: every WAV becomes a FLAC next to it, the File column of the
   detection CSVs is pointed at the FLAC files, then the WAV files are deleted.
2. Sort: every recording moves into the week folder the importer would choose.

    uv run scripts/convert_old_projects.py /path/to/project --dry-run
    uv run scripts/convert_old_projects.py /path/to/project

Each step does nothing for files already in the current layout, so the script
can be run again after an interruption or on a project that is half done.

Files are handled like the importer handles them: a WAV that is not 16-bit PCM
has no lossless FLAC form and is kept, a WAV with no audio samples is deleted,
and an undated recording goes to week_01.

Sorting only happens in campaigns without detection CSVs. A detection's Week
and Species_List cells record which species list its file was filtered
against, and moving the file would contradict them. In a campaign with CSVs
the misplaced files are reported, and sorting them takes deleting the CSVs
and analysing again.

Close the project in PAM Analyzer first, or a later save from the app can
overwrite the updated CSVs with its in-memory copy.

With --audio-only the folder is not a project but any tree of recordings,
such as offloaded SD cards. Only the convert step runs, without CSVs to
update, and each WAV is deleted as soon as its own FLAC is verified, so an
interrupted run resumes where it stopped.

    uv run scripts/convert_old_projects.py /path/to/recordings --audio-only
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from functools import partial
from pathlib import Path, PurePosixPath, PureWindowsPath

import polars as pl
import soundfile as sf

from pam_analyzer.domain import detection_schema, paths
from pam_analyzer.domain.audio_import import birdnet_week
from pam_analyzer.domain.detection_store import read_detections_csv, write_detections_frame
from pam_analyzer.infrastructure.audio_import import (
    EmptyRecordingError,
    NotLosslessError,
    _resolve_source_time,  # pyright: ignore[reportPrivateUsage]
    transcode_to_flac,
)


class Outcome(Enum):
    CONVERTED = "converted"
    EMPTY = "empty"
    NOT_LOSSLESS = "not 16-bit PCM, kept"
    FAILED = "failed"


@dataclass(frozen=True)
class FileResult:
    wav: Path
    outcome: Outcome
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Move:
    src: Path
    dst: Path


@dataclass
class CampaignSummary:
    converted: list[Path] = field(default_factory=list)
    empty: list[Path] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    failed: list[FileResult] = field(default_factory=list)
    csv_failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    moved: list[Move] = field(default_factory=list)
    misplaced: list[Move] = field(default_factory=list)
    move_failures: list[str] = field(default_factory=list)


def campaign_folders(project: Path) -> list[Path]:
    return sorted(d for d in project.iterdir() if d.is_dir() and (d / "campaign.toml").is_file())


def audio_files(campaign: Path, suffixes: frozenset[str] = paths.AUDIO_EXTENSIONS) -> list[Path]:
    """Audio under *campaign* at any depth, the same set an analysis run reads.

    os.walk rather than rglob plus is_file, because the directory listing
    already tells files from folders. The extra stat per file costs a round
    trip each on an SMB share.
    """
    found: list[Path] = []
    for folder, _subfolders, names in os.walk(campaign):
        found.extend(Path(folder, n) for n in names if os.path.splitext(n)[1].lower() in suffixes)
    return sorted(found)


def flac_sibling(wav: Path) -> Path:
    return wav.with_name(wav.stem + ".flac")


def convert_one(wav: Path, delete: bool = False) -> FileResult:
    """Transcode one WAV next to itself, deleting it afterwards when *delete* is set.

    Runs in a worker process. Processes rather than threads, because
    libsndfile is not thread-safe on Windows and each process loads its own
    copy. transcode_to_flac raises before the FLAC is in place unless the
    round trip was bit-exact, so a deleted WAV always has a verified FLAC.
    """
    try:
        resolved, notes = _resolve_source_time(wav)
        transcode_to_flac(wav, flac_sibling(wav), resolved)
        if delete:
            wav.unlink()
    except EmptyRecordingError:
        if delete:
            wav.unlink(missing_ok=True)
        return FileResult(wav, Outcome.EMPTY)
    except NotLosslessError as exc:
        return FileResult(wav, Outcome.NOT_LOSSLESS, (exc.subtype,))
    except Exception as exc:
        return FileResult(wav, Outcome.FAILED, (f"{wav.name}: {exc}",))
    return FileResult(wav, Outcome.CONVERTED, tuple(notes))


def classify_dry_run(wav: Path) -> FileResult:
    """What convert_one would do, from the header alone."""
    try:
        info = sf.info(wav)
    except Exception as exc:
        return FileResult(wav, Outcome.FAILED, (f"{wav.name}: {exc}",))
    if info.frames == 0:
        return FileResult(wav, Outcome.EMPTY)
    if info.subtype != "PCM_16":
        return FileResult(wav, Outcome.NOT_LOSSLESS, (info.subtype,))
    return FileResult(wav, Outcome.CONVERTED)


def _file_path(cell: str, project: Path) -> Path:
    """Where a project-relative or absolute File cell points on this machine."""
    pure = PureWindowsPath(cell) if "\\" in cell else PurePosixPath(cell)
    return Path(pure) if pure.is_absolute() else project.joinpath(*pure.parts)


def _flac_cell(cell: str) -> str:
    """*cell* with its '.wav' suffix swapped, keeping the separator style it was written with."""
    return cell[: -len(".wav")] + ".flac"


def flac_renames(files: pl.Series, project: Path, pending: frozenset[Path] = frozenset()) -> dict[str, str]:
    """File cells that name a WAV whose FLAC exists, mapped to the FLAC cell.

    Decided by the files on disk rather than by this run's results, so a
    rerun after an interruption still updates rows of WAVs converted earlier.
    *pending* holds WAVs a dry run counts as converted.
    """
    renames: dict[str, str] = {}
    for cell in files.unique().to_list():
        if not cell or not cell.lower().endswith(".wav"):
            continue
        wav = _file_path(cell, project)
        if wav in pending or flac_sibling(wav).is_file():
            renames[cell] = _flac_cell(cell)
    return renames


def update_csv(path: Path, project: Path, pending: frozenset[Path] | None) -> int:
    """Point *path*'s File cells at the FLAC files and return the rows changed.

    A dry run passes the WAVs it would convert as *pending* and writes nothing.
    Goes through the app's own reader and writer, so the rewritten file is
    byte-identical to what a save from the app would produce.
    """
    frame, fieldnames = read_detections_csv(path)
    if "File" not in fieldnames or frame.is_empty():
        return 0
    renames = flac_renames(frame["File"], project, pending or frozenset())
    changed = int(frame["File"].is_in(list(renames)).sum()) if renames else 0
    if changed and pending is None:
        frame = frame.with_columns(pl.col("File").replace(renames))
        write_detections_frame(path, frame, fieldnames)
    return changed


def _duration(seconds: float) -> str:
    minutes = int(seconds) // 60
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _ignore_interrupt() -> None:
    """Worker initializer that leaves Ctrl+C to the main process.

    Otherwise every worker dies mid-file on Ctrl+C, and the pool's shutdown
    then waits forever on workers that are gone.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _run_files(wavs: list[Path], dry_run: bool, jobs: int, delete: bool = False) -> list[FileResult]:
    """Run the convert step, or its dry run, over *wavs* in parallel with a progress line.

    The dry run is parallel too, because on a network share each header read
    is a round trip, and those dominate over the work itself.
    """
    work = classify_dry_run if dry_run else partial(convert_one, delete=delete)
    # A header read is too short to pay for one round trip to a worker each.
    chunksize = 16 if dry_run else 1
    results: list[FileResult] = []
    interactive = sys.stdout.isatty()
    start = time.monotonic()
    first_done = start
    pool = ProcessPoolExecutor(max_workers=jobs, initializer=_ignore_interrupt)
    try:
        for done, result in enumerate(pool.map(work, wavs, chunksize=chunksize), start=1):
            results.append(result)
            if not interactive:
                continue
            now = time.monotonic()
            if done == 1:
                # The rate counts from here, so the workers' start-up does not inflate the estimate.
                first_done = now
                left = "..."
            else:
                left = "~" + _duration((now - first_done) / (done - 1) * (len(wavs) - done))
            print(f"\r  {done}/{len(wavs)} elapsed {_duration(now - start)} left {left} "
                  f"{result.wav.name}\033[K", end="", flush=True)
    except KeyboardInterrupt:
        print(f"\nStopping after the {jobs} files in progress...", flush=True)
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown()
    if interactive:
        print("\r\033[K", end="")
    return results


def convert_folder(folder: Path, *, dry_run: bool, keep_wav: bool, jobs: int) -> CampaignSummary:
    """The convert step alone over every WAV under a folder that is not a project."""
    summary = CampaignSummary()
    print("Listing WAV files...")
    wavs = audio_files(folder, frozenset({".wav"}))
    print(f"{len(wavs)} WAV files")
    for result in _run_files(wavs, dry_run, jobs, delete=not keep_wav):
        match result.outcome:
            case Outcome.CONVERTED:
                summary.converted.append(result.wav)
                summary.notes.extend(result.notes)
            case Outcome.EMPTY:
                summary.empty.append(result.wav)
            case Outcome.NOT_LOSSLESS:
                summary.kept.append(f"{result.wav.relative_to(folder)} ({result.notes[0]})")
            case Outcome.FAILED:
                summary.failed.append(result)
    return summary


def convert_campaign(campaign: Path, project: Path, summary: CampaignSummary, *, dry_run: bool, keep_wav: bool,
                     jobs: int) -> None:
    for result in _run_files(audio_files(campaign, frozenset({".wav"})), dry_run, jobs):
        match result.outcome:
            case Outcome.CONVERTED:
                summary.converted.append(result.wav)
                summary.notes.extend(result.notes)
            case Outcome.EMPTY:
                summary.empty.append(result.wav)
            case Outcome.NOT_LOSSLESS:
                summary.kept.append(f"{result.wav.name} ({result.notes[0]})")
            case Outcome.FAILED:
                summary.failed.append(result)

    verb = "would point" if dry_run else "points"
    pending = frozenset(summary.converted) if dry_run else None
    for csv in detection_schema.campaign_csvs(campaign):
        try:
            changed = update_csv(csv, project, pending)
        except Exception as exc:
            summary.csv_failures.append(f"{csv.name}: {exc}")
            continue
        if changed:
            print(f"  {csv.name}: {verb} {changed} rows at FLAC files")

    # A CSV that was not updated still names the WAV files, so they must stay for it to resolve.
    if not dry_run and not keep_wav and not summary.csv_failures:
        for wav in summary.converted + summary.empty:
            wav.unlink(missing_ok=True)


def week_target(audio: Path, campaign: Path) -> Path | None:
    """Where the importer would put *audio*: <ARU>/week_NN/<name>.

    None for a file directly in the campaign folder, which belongs to no ARU.
    """
    parts = audio.relative_to(campaign).parts
    if len(parts) < 2:
        return None
    resolved, _ = _resolve_source_time(audio)
    week = birdnet_week(resolved.time) if resolved is not None else 1
    return campaign / parts[0] / f"week_{week:02d}" / audio.name


def _remove_empty_dirs(start: Path, stop: Path) -> None:
    """rmdir *start* and its parents below *stop* for as long as they are empty."""
    folder = start
    while folder != stop and stop in folder.parents:
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def _files_to_sort(campaign: Path, summary: CampaignSummary, dry_run: bool, keep_wav: bool) -> list[tuple[Path, Path]]:
    """(file to move, file to read its recording time from) for each recording.

    A real run reads both from the same file. A dry run has not converted
    anything, so it sorts the FLAC files the convert step would write by the
    time of their WAV, and leaves out the WAV files that step would delete.
    """
    files = audio_files(campaign)
    if not dry_run:
        return [(f, f) for f in files]
    converted = set(summary.converted)
    deleted = set() if keep_wav else converted | set(summary.empty)
    pairs = [(flac_sibling(f), f) for f in files if f in converted]
    pairs += [(f, f) for f in files if f not in deleted]
    return pairs


def sort_campaign(campaign: Path, summary: CampaignSummary, *, dry_run: bool, keep_wav: bool) -> None:
    has_csvs = bool(detection_schema.campaign_csvs(campaign))
    for audio, source in _files_to_sort(campaign, summary, dry_run, keep_wav):
        try:
            target = week_target(source, campaign)
        except Exception as exc:
            summary.move_failures.append(f"{audio.name}: {exc}")
            continue
        if target is None:
            summary.move_failures.append(f"{audio.name}: in the campaign folder itself, not in an ARU folder")
            continue
        target = target.with_name(audio.name)
        if target == audio:
            continue
        move = Move(audio, target)
        if has_csvs:
            summary.misplaced.append(move)
        elif target.exists():
            summary.move_failures.append(f"{audio.name}: {target.relative_to(campaign)} already exists")
        elif dry_run:
            summary.moved.append(move)
        else:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                audio.rename(target)
            except OSError as exc:
                summary.move_failures.append(f"{audio.name}: {exc}")
                continue
            summary.moved.append(move)
            _remove_empty_dirs(audio.parent, campaign / target.relative_to(campaign).parts[0])


def _print_summary(campaign: Path, summary: CampaignSummary, dry_run: bool, keep_wav: bool,
                   audio_only: bool = False) -> None:
    converted = "would convert" if dry_run else "converted"
    deleted = "kept" if keep_wav else ("would delete" if dry_run else "deleted")
    sorted_ = "would sort" if dry_run else "sorted"
    print(f"  {converted} {len(summary.converted)} WAV files, empty recordings {deleted}: {len(summary.empty)}")
    if not audio_only:
        print(f"  {sorted_} {len(summary.moved)} files into week folders")
    for kept in summary.kept:
        print(f"  kept as WAV, not 16-bit PCM: {kept}")
    for note in summary.notes:
        print(f"  note: {note}")
    if summary.misplaced:
        print(f"  not sorted, the campaign has detection CSVs: {len(summary.misplaced)} files outside their week folder")
        for move in summary.misplaced:
            print(f"    {move.src.relative_to(campaign)} belongs in {move.dst.parent.relative_to(campaign)}")
    for failure in summary.failed:
        for note in failure.notes:
            print(f"  failed: {note}", file=sys.stderr)
    for failure in summary.csv_failures:
        print(f"  CSV not updated, WAV files kept: {failure}", file=sys.stderr)
    for failure in summary.move_failures:
        print(f"  not sorted: {failure}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("project", type=Path, help="project folder (the one holding pam-analyzer.toml)")
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing")
    parser.add_argument("--keep-wav", action="store_true", help="leave the WAV files next to their FLAC files")
    parser.add_argument(
        "--jobs", type=int, default=os.cpu_count() or 1, help="parallel transcodes (default: CPU count)"
    )
    parser.add_argument(
        "--audio-only", action="store_true", help="convert WAV under a folder that is not a project, nothing else"
    )
    args = parser.parse_args(argv)
    # Keeps stderr failures next to the stdout lines they belong to when piped to a log.
    sys.stdout.reconfigure(line_buffering=True)  # pyright: ignore[reportAttributeAccessIssue]

    project: Path = args.project.resolve()
    if not project.is_dir():
        print(f"Not a folder: {project}", file=sys.stderr)
        return 1
    campaigns = campaign_folders(project)

    if args.audio_only:
        # Renaming a project's WAV files without its CSVs would leave detections pointing at nothing.
        if campaigns or paths.project_toml(project).exists():
            print(f"{project} is a PAM Analyzer project, run without --audio-only", file=sys.stderr)
            return 1
        summary = convert_folder(project, dry_run=args.dry_run, keep_wav=args.keep_wav, jobs=max(1, args.jobs))
        _print_summary(project, summary, args.dry_run, args.keep_wav, audio_only=True)
        return 1 if summary.failed else 0

    if not campaigns:
        print(f"No campaign folders under {project}", file=sys.stderr)
        return 1

    any_failed = False
    for campaign in campaigns:
        print(campaign.name)
        summary = CampaignSummary()
        convert_campaign(campaign, project, summary, dry_run=args.dry_run, keep_wav=args.keep_wav,
                         jobs=max(1, args.jobs))
        sort_campaign(campaign, summary, dry_run=args.dry_run, keep_wav=args.keep_wav)
        _print_summary(campaign, summary, args.dry_run, args.keep_wav)
        any_failed = any_failed or bool(summary.failed or summary.csv_failures or summary.move_failures)
    return 1 if any_failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Stopped. Run the same command again to continue.", file=sys.stderr)
        sys.exit(130)
