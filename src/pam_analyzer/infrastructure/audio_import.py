"""Copies SD card files into campaign folders by BirdNET week."""

import shutil
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import guano

from ..domain.audio_import import (
    CardImportResult,
    ConflictChoice,
    ConflictReport,
    DetectedCard,
    FileConflict,
    ImportProgress,
    birdnet_week,
)


def extract_recording_time(path: Path) -> datetime:
    """Return a recording timestamp for a WAV file.

    Priority: GUANO metadata, filename YYYYMMDD_HHMMSS, file mtime.
    """
    try:
        ts = guano.GuanoFile(str(path)).get("Timestamp")
        if ts is not None:
            return ts
    except Exception:
        pass
    try:
        return datetime.strptime(path.stem, "%Y%m%d_%H%M%S")
    except ValueError:
        pass
    return datetime.fromtimestamp(path.stat().st_mtime)


class AudioImporter:
    def list_card_files(self, card_root: Path) -> list[Path]:
        """Return sorted top-level WAV (case-insensitive) and CONFIG.TXT files."""
        files = [
            f
            for f in card_root.iterdir()
            if f.is_file() and ((n := f.name.upper()).endswith(".WAV") or n == "CONFIG.TXT")
        ]
        return sorted(files)

    def detect_conflicts(self, files: list[Path], dest_dir: Path) -> ConflictReport:
        """Walk dest_dir once and classify each source file as identical or a genuine conflict."""
        dest_map: dict[str, Path] = {}
        if dest_dir.exists():
            for p in dest_dir.rglob("*"):
                if p.is_file():
                    dest_map[p.name] = p

        conflicts: list[FileConflict] = []
        identical: list[str] = []
        for src in files:
            dst = dest_map.get(src.name)
            if dst is None:
                continue
            src_stat = src.stat()
            dst_stat = dst.stat()
            if src_stat.st_size == dst_stat.st_size and abs(src_stat.st_mtime - dst_stat.st_mtime) <= 2:
                identical.append(src.name)
            else:
                conflicts.append(
                    FileConflict(
                        filename=src.name,
                        src_size=src_stat.st_size,
                        src_mtime=src_stat.st_mtime,
                        dst_size=dst_stat.st_size,
                        dst_mtime=dst_stat.st_mtime,
                        dst_path=dst,
                    )
                )
        return ConflictReport(conflicts=tuple(conflicts), identical=tuple(identical))

    def import_card(
        self,
        card: DetectedCard,
        files: list[Path],
        dest_dir: Path,
        resolutions: dict[str, ConflictChoice],
        identical: tuple[str, ...],
        *,
        progress: Callable[[ImportProgress], None],
        is_cancelled: Callable[[], bool],
        clear_after: bool,
    ) -> CardImportResult:
        """Copy files from card to dest_dir, honoring conflict resolutions and week placement."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        files_copied = 0
        files_skipped = 0
        bytes_copied = 0
        error = ""
        identical_set = set(identical)
        copied_sources: list[Path] = []

        try:
            for src in files:
                if is_cancelled():
                    error = "Cancelled"
                    break

                filename = src.name

                if filename in identical_set:
                    files_skipped += 1
                    progress(
                        ImportProgress(
                            card=card,
                            files_done=files_copied + files_skipped,
                            files_total=len(files),
                            bytes_done=bytes_copied,
                            elapsed=time.monotonic() - start,
                        )
                    )
                    continue

                if filename in resolutions and resolutions[filename] == ConflictChoice.SKIP:
                    files_skipped += 1
                    progress(
                        ImportProgress(
                            card=card,
                            files_done=files_copied + files_skipped,
                            files_total=len(files),
                            bytes_done=bytes_copied,
                            elapsed=time.monotonic() - start,
                        )
                    )
                    continue

                if filename.upper() == "CONFIG.TXT":
                    dst = dest_dir / filename
                else:
                    try:
                        ts = extract_recording_time(src)
                        week = birdnet_week(ts)
                    except Exception:
                        week = 1
                    week_dir = dest_dir / f"week_{week:02d}"
                    week_dir.mkdir(parents=True, exist_ok=True)
                    dst = week_dir / filename

                shutil.copy2(src, dst)
                size = src.stat().st_size
                files_copied += 1
                bytes_copied += size
                copied_sources.append(src)
                progress(
                    ImportProgress(
                        card=card,
                        files_done=files_copied + files_skipped,
                        files_total=len(files),
                        bytes_done=bytes_copied,
                        elapsed=time.monotonic() - start,
                    )
                )
        except Exception as exc:
            error = str(exc)

        elapsed = time.monotonic() - start

        if clear_after and not error:
            for src in copied_sources:
                try:
                    src.unlink()
                except Exception:
                    pass

        return CardImportResult(
            card=card,
            files_copied=files_copied,
            files_skipped=files_skipped,
            bytes_copied=bytes_copied,
            elapsed=elapsed,
            error=error,
            dest_dir=dest_dir if (files_copied > 0 or files_skipped > 0) else None,
        )
