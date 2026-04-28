"""Value objects and pure logic for SD card audio import."""

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path


@dataclass(frozen=True)
class DetectedCard:
    name: str
    mountpoint: Path
    device: str


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


def birdnet_week(dt: datetime) -> int:
    """Return the BirdNET week number [1-48] for a datetime."""
    return min(48, (dt.month - 1) * 4 + math.ceil(dt.day / 7))


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
