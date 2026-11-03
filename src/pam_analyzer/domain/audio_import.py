"""Value objects and pure logic for SD card audio import."""

import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import ClassVar

# Digit boundaries, so a recorder serial ending in digits can't donate the
# first half of a false match like "64876836_202605".
_RECORDING_TIME_RE = re.compile(r"(?<!\d)(\d{8}_\d{6})(?!\d)")


class ImportSource(Enum):
    SD_CARD = "sd_card"
    FOLDER = "folder"


@dataclass(frozen=True)
class DetectedCard:
    name: str
    mountpoint: Path
    device: str
    source: ImportSource = ImportSource.SD_CARD


class ConflictChoice(Enum):
    SKIP = "skip"
    REPLACE = "replace"


@dataclass(frozen=True)
class FileConflict:
    filename: str
    src_size: int
    src_mtime: float
    dst_size: int
    dst_mtime: float
    dst_path: Path


@dataclass(frozen=True)
class ConflictReport:
    conflicts: tuple[FileConflict, ...]
    identical: tuple[str, ...]  # filenames auto-skipped because size + mtime match


@dataclass(frozen=True)
class ImportProgress:
    card: DetectedCard
    files_done: int
    files_total: int
    bytes_done: int
    elapsed: float


@dataclass(frozen=True)
class CardImportResult:
    card: DetectedCard
    files_copied: int
    files_skipped: int
    bytes_copied: int
    elapsed: float
    error: str
    dest_dir: Path | None
    # Files imported despite a GUANO/filename mismatch, one message each.
    warnings: tuple[str, ...] = ()

    CANCELLED: ClassVar[str] = "Cancelled"

    @property
    def cancelled(self) -> bool:
        return self.error == self.CANCELLED


# Sentinel week for recordings outside any week_NN folder. Mirrors the
# birdnet geo API, where week=-1 requests the year-round species list
# instead of a seasonal one; the same value is written to the detections
# CSV Week column so a row's provenance stays visible.
WEEK_YEAR_ROUND = -1


def birdnet_week(dt: datetime) -> int:
    """Return the BirdNET week number [1-48] for a datetime."""
    return min(48, (dt.month - 1) * 4 + math.ceil(dt.day / 7))


def week_from_path(path: Path) -> int | None:
    """Extract the BirdNET week number from a 'week_NN' path segment, or None."""
    for part in path.parts:
        if part.startswith("week_"):
            try:
                return int(part.split("_", 1)[1])
            except (IndexError, ValueError):
                pass
    return None


def parse_recording_time(stem: str) -> datetime | None:
    """Pull a 'YYYYMMDD_HHMMSS' timestamp out of an audio filename."""
    match = _RECORDING_TIME_RE.search(stem)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
        except ValueError:
            pass
    return None


class TimeSource(Enum):
    GUANO = "guano"
    FILENAME = "filename"
    MTIME = "mtime"


@dataclass(frozen=True)
class ResolvedTime:
    """A recording's start as recorder wall-clock time, without UTC offset.

    filename_conflict is True when GUANO and the filename both carry a time
    and the two wall-clock values differ.
    """

    time: datetime
    source: TimeSource
    filename_conflict: bool = False


def resolve_recording_time(
    guano_time: datetime | None, stem: str, mtime_start: datetime | None = None
) -> ResolvedTime | None:
    """Pick a recording's start time from GUANO, the filename, or mtime, in that order.

    The GUANO offset is dropped, not converted, so the result matches the
    filename stamp, which recorders write in the same wall clock.
    """
    from_name = parse_recording_time(stem)
    if guano_time is not None:
        wall = guano_time.replace(tzinfo=None)
        return ResolvedTime(wall, TimeSource.GUANO, from_name is not None and from_name != wall)
    if from_name is not None:
        return ResolvedTime(from_name, TimeSource.FILENAME)
    if mtime_start is not None:
        return ResolvedTime(mtime_start, TimeSource.MTIME)
    return None


def format_recording_time(time: datetime | None) -> str:
    """The Recording_Time CSV cell, or "" when the time is unknown."""
    return time.strftime("%Y-%m-%d %H:%M:%S") if time is not None else ""


def same_original_name(original: str, name: str) -> bool:
    """True if a GUANO 'Original Filename' names the file called *name*.

    Compares stems case-insensitively, so a FLAC transcoded from the named WAV
    still matches. Windows separators are handled whatever the host OS.
    """
    return PureWindowsPath(original).stem.lower() == PureWindowsPath(name).stem.lower()


def date_range_from_stems(stems: Iterable[str]) -> tuple[datetime, datetime] | None:
    """Earliest and latest recording time parsed from filename stems.

    Returns None when no stem carries a parseable timestamp, so callers can
    render a blank range for recorders that don't stamp the time into names.
    """
    times = [t for stem in stems if (t := parse_recording_time(stem)) is not None]
    if not times:
        return None
    return min(times), max(times)


def merge_date_ranges(
    ranges: Iterable[tuple[datetime, datetime] | None],
) -> tuple[datetime, datetime] | None:
    """Combine several date ranges (e.g. per week/card/campaign) into their overall span."""
    valid = [r for r in ranges if r is not None]
    if not valid:
        return None
    return min(r[0] for r in valid), max(r[1] for r in valid)


def discover_folder_cards(
    root: Path, has_direct_audio: Callable[[Path], bool]
) -> list[DetectedCard]:
    """Propose one or more cards for a manually picked import folder.

    If root itself qualifies per has_direct_audio, it is treated as a single
    card named after root (its recordings may still sit in per-day subfolders
    one level down; building that file list is list_card_files' job, not this
    function's). Otherwise each immediate subdirectory that qualifies becomes
    its own card, named after that subdirectory, mirroring a folder of several
    already-offloaded SD cards.
    """
    if has_direct_audio(root):
        return [DetectedCard(name=root.name, mountpoint=root, device="", source=ImportSource.FOLDER)]
    cards: list[DetectedCard] = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and has_direct_audio(entry):
            cards.append(DetectedCard(name=entry.name, mountpoint=entry, device="", source=ImportSource.FOLDER))
    return cards


class CardQueue:
    """FIFO queue for DetectedCards with dedup by card name."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._queue: list[DetectedCard] = []

    def offer(self, cards: list[DetectedCard]) -> None:
        """Add cards not yet seen to the back of the queue."""
        for card in cards:
            if card.name not in self._seen:
                self._seen.add(card.name)
                self._queue.append(card)

    def pop(self) -> DetectedCard | None:
        """Remove and return the next card, or None if the queue is empty."""
        return self._queue.pop(0) if self._queue else None

    def clear_seen(self) -> None:
        """Allow previously-seen cards to be re-offered (e.g. when campaign changes)."""
        self._seen.clear()

    def reset(self) -> None:
        """Clear both queue and seen set (e.g. on watch session start)."""
        self._seen.clear()
        self._queue.clear()

    @property
    def pending(self) -> list[DetectedCard]:
        return list(self._queue)
