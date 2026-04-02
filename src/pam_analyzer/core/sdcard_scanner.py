"""SD card scanning and file transfer helpers.

Encapsulates SD card detection state and provides pure utility functions
for the copy workflow. No NiceGUI dependency, safe to test in isolation.
"""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import psutil

from pam_analyzer.core.utils import get_volume_name


@dataclass
class CardResult:
    """Result of copying one SD card's files to a campaign folder."""

    card: str
    files_copied: int = 0
    files_skipped: int = 0
    bytes_copied: int = 0
    elapsed: float = 0.0
    error: str = ''
    dest_dir: Path | None = None


class SDCardScanner:
    """Scans for mounted SD cards matching a name pattern and manages a FIFO queue."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.queue: list[tuple[str, str, str]] = []  # (card_name, mountpoint, device)

    def poll(self, sdcard_pattern: str) -> list[tuple[str, str, str]]:
        """Scan disk partitions for new SD cards matching the pattern.

        Returns a list of newly found ``(name, mountpoint, device)`` tuples
        that were added to the queue.
        """
        try:
            pattern = re.compile(sdcard_pattern, re.IGNORECASE)
        except re.error:
            return []

        new_cards: list[tuple[str, str, str]] = []
        for partition in psutil.disk_partitions():
            try:
                name = get_volume_name(partition)
            except Exception:
                continue
            if not name:
                continue
            if pattern.search(name) and name not in self.seen:
                self.seen.add(name)
                entry = (name, partition.mountpoint, partition.device)
                self.queue.append(entry)
                new_cards.append(entry)

        return new_cards

    def pop_next(self) -> tuple[str, str, str] | None:
        """Remove and return the next card from the queue, or None if empty."""
        return self.queue.pop(0) if self.queue else None

    def clear_seen(self) -> None:
        """Clear the set of seen card names so the same cards can be detected again."""
        self.seen.clear()

    def forget(self, card_name: str) -> None:
        """Remove a single card from seen so it can be detected again after re-insertion."""
        self.seen.discard(card_name)

    def reset(self) -> None:
        """Clear seen cards and queue (e.g. when starting a new watch session)."""
        self.seen.clear()
        self.queue.clear()

    @property
    def has_pending(self) -> bool:
        return bool(self.queue)


def files_are_identical(src_stat, dest_stat) -> bool:
    """Check if source and destination files appear identical (same size, close mtime)."""
    return src_stat.st_size == dest_stat.st_size and abs(src_stat.st_mtime - dest_stat.st_mtime) < 2


def resolve_dest_path(src: Path, dest_dir: Path, week: int) -> Path:
    """Determine the destination path for a file being copied from an SD card.

    CONFIG.TXT goes directly into dest_dir; WAV files go into ``week_NN/`` subdirs.
    Creates the week directory if needed.
    """
    if src.name.upper() == 'CONFIG.TXT':
        return dest_dir / src.name
    week_dir = dest_dir / f'week_{week:02d}'
    week_dir.mkdir(parents=True, exist_ok=True)
    return week_dir / src.name


def copy_file(src: Path, dest: Path) -> int:
    """Copy a file preserving metadata. Returns bytes copied."""
    shutil.copy2(src, dest)
    return src.stat().st_size
