"""Copies SD card files into campaign folders by BirdNET week.

WAV recordings are transcoded to FLAC on import (lossless, 16-bit PCM) to save
disk space; FLAC sources from the card and CONFIG.TXT are copied unchanged.
"""

import os
import shutil
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import guano
import numpy as np
import soundfile as sf

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


class NotLosslessError(Exception):
    """Raised when a source cannot be transcoded to FLAC without loss.

    FLAC via libsndfile stores integer PCM only, so a 32-bit float (or any
    non-PCM_16) source has no lossless FLAC representation. The importer
    catches this and byte-copies the original instead of degrading it.
    """

    def __init__(self, subtype: str) -> None:
        super().__init__(f"non-lossless source subtype {subtype!r}")
        self.subtype = subtype


class VerifyError(Exception):
    """Raised when a freshly written FLAC does not decode back to its source.

    Guards the 'clear card after import' path: a source is only deleted from
    the card once its FLAC has been proven bit-identical, so a corrupt encode
    can never cost the only copy of a recording.
    """


def _flac_target_name(src: Path) -> str:
    """Destination filename for *src* once imported (WAV becomes .flac)."""
    return src.stem + ".flac"


def transcode_to_flac(src: Path, dst: Path) -> int:
    """Losslessly transcode a 16-bit PCM WAV at *src* to FLAC at *dst*.

    Writes to a same-directory temp file, decodes it back, and asserts the
    samples match the source before atomically moving it into place. The temp
    name ends in '.part' (not '.flac') so a hard crash cannot leave a stray
    file that inventory discovery or analysis would mistake for a recording.

    Returns the number of frames written. Raises NotLosslessError if the
    source is not PCM_16, or VerifyError if the round-trip is not bit-exact;
    in both cases *dst* is left absent.
    """
    info = sf.info(src)
    if info.subtype != "PCM_16":
        raise NotLosslessError(info.subtype)

    audio, _ = sf.read(src, dtype="int16")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    try:
        sf.write(tmp, audio, info.samplerate, subtype="PCM_16", format="FLAC")
        # libsndfile sniffs the format from the header on read, so the '.part'
        # extension is fine and passing format= would be rejected for an
        # existing file.
        decoded, _ = sf.read(tmp, dtype="int16")
        if not np.array_equal(decoded, audio):
            raise VerifyError(f"FLAC verify mismatch for {dst.name}")
        os.replace(tmp, dst)
    finally:
        # On success os.replace consumed tmp; on any failure discard the partial.
        Path(tmp).unlink(missing_ok=True)
    return len(audio)


def _byte_identical(src: Path, dst: Path) -> bool:
    """True if two files have the same size and near-identical mtime.

    The cheap heuristic used for byte-for-byte copies (CONFIG.TXT, FLAC from
    the card): shutil.copy2 preserves mtime, so size + mtime within 2s is a
    strong 'already copied' signal without hashing.
    """
    src_stat = src.stat()
    dst_stat = dst.stat()
    return src_stat.st_size == dst_stat.st_size and abs(src_stat.st_mtime - dst_stat.st_mtime) <= 2


def _is_decodable(path: Path) -> bool:
    """True if soundfile can read *path*'s header.

    Used to confirm an existing FLAC target is a complete recording before we
    skip re-importing it. Atomic writes make a corrupt target nearly
    impossible, but a leftover from external tampering should be re-imported
    rather than trusted.
    """
    try:
        sf.info(path)
    except Exception:
        return False
    return True


@dataclass(frozen=True)
class _FileOutcome:
    """Result of processing one source file on a worker thread.

    error is empty on success. size is the source byte count, reported to the
    progress callback so the bar measures progress through the card (not the
    smaller transcoded output).
    """

    src: Path
    size: int
    error: str


class AudioImporter:
    def list_card_files(self, card_root: Path) -> list[Path]:
        """Return sorted top-level WAV/FLAC (case-insensitive) and CONFIG.TXT files.

        WAV is transcoded to FLAC on import; FLAC already on the card is copied
        through unchanged.
        """
        files = [
            f
            for f in card_root.iterdir()
            if f.is_file() and ((n := f.name.upper()).endswith((".WAV", ".FLAC")) or n == "CONFIG.TXT")
        ]
        return sorted(files)

    def detect_conflicts(self, files: list[Path], dest_dir: Path) -> ConflictReport:
        """Walk dest_dir once and classify each source as already-imported or a conflict.

        WAV sources are matched against their transcoded '.flac' target: a
        target that exists and decodes means already imported (auto-skip). A
        same-named WAV in dest (a legacy byte-copy import, or a float source
        that fell back to copy) that still byte-matches also counts as
        imported, so re-inserting a card neither re-transcodes nor leaves a
        WAV/FLAC duplicate. WAV sources never raise a user conflict because the
        import overwrites any stale target atomically.

        Passthrough sources (FLAC from the card, CONFIG.TXT) keep their name
        and use the byte-identical check: a match is auto-skipped, a mismatch
        is a genuine conflict the user resolves.

        identical entries are keyed by source filename, matching what
        import_card and the orchestrator expect.
        """
        dest_map: dict[str, Path] = {}
        if dest_dir.exists():
            for p in dest_dir.rglob("*"):
                if p.is_file():
                    dest_map[p.name] = p

        conflicts: list[FileConflict] = []
        identical: list[str] = []
        for src in files:
            if src.name.upper().endswith(".WAV"):
                flac_dst = dest_map.get(_flac_target_name(src))
                same_name_dst = dest_map.get(src.name)
                if (flac_dst is not None and _is_decodable(flac_dst)) or (
                    same_name_dst is not None and _byte_identical(src, same_name_dst)
                ):
                    identical.append(src.name)
                # Otherwise the import proceeds and overwrites any stale target;
                # a WAV source is never surfaced as a user-facing conflict.
                continue

            # Passthrough: FLAC from the card or CONFIG.TXT, copied under its name.
            dst = dest_map.get(src.name)
            if dst is None:
                continue
            if _byte_identical(src, dst):
                identical.append(src.name)
            else:
                src_stat = src.stat()
                dst_stat = dst.stat()
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
        """Transcode/copy files from card to dest_dir, honoring conflicts and week placement.

        WAV is transcoded to FLAC on worker threads (libsndfile releases the
        GIL, so this scales across cores); FLAC and CONFIG.TXT are copied
        unchanged. Skips are decided up front so workers only ever do real
        work, and counters are aggregated on this thread as futures complete,
        so no locking is needed.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        identical_set = set(identical)

        def _skipped(src: Path) -> bool:
            return src.name in identical_set or resolutions.get(src.name) == ConflictChoice.SKIP

        to_process = [s for s in files if not _skipped(s)]
        files_skipped = len(files) - len(to_process)

        files_copied = 0
        bytes_copied = 0
        copied_sources: list[Path] = []
        errors: list[str] = []
        error = ""

        def _emit() -> None:
            progress(
                ImportProgress(
                    card=card,
                    files_done=files_copied + files_skipped,
                    files_total=len(files),
                    bytes_done=bytes_copied,
                    elapsed=time.monotonic() - start,
                )
            )

        _emit()  # reflect auto-skips immediately, before the slow work starts

        if to_process:
            max_workers = min(os.cpu_count() or 4, len(to_process))
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futures = {ex.submit(self._process_one, src, dest_dir): src for src in to_process}
                try:
                    for fut in as_completed(futures):
                        if is_cancelled():
                            error = "Cancelled"
                            for f in futures:
                                f.cancel()
                            break
                        outcome = fut.result()
                        if outcome.error:
                            errors.append(outcome.error)
                        else:
                            files_copied += 1
                            bytes_copied += outcome.size
                            copied_sources.append(outcome.src)
                        _emit()
                except Exception as exc:  # noqa: BLE001
                    error = str(exc)

        if not error and errors:
            error = errors[0]

        elapsed = time.monotonic() - start

        # Clear the card only on a fully clean run. copied_sources holds only
        # verified imports, but leaving the whole card intact when anything
        # failed or was cancelled is the safer policy for a field tool.
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

    def _process_one(self, src: Path, dest_dir: Path) -> _FileOutcome:
        """Copy or transcode one source into dest_dir. Pure: touches no shared state.

        Runs on a worker thread. WAV becomes FLAC (or a byte-copy if it is not
        16-bit PCM and so has no lossless FLAC form); FLAC from the card and
        CONFIG.TXT are copied as-is. Any exception (including a failed FLAC
        verify) is returned as an error rather than raised, so one bad file
        does not abort the rest of the card.
        """
        try:
            size = src.stat().st_size

            if src.name.upper() == "CONFIG.TXT":
                shutil.copy2(src, dest_dir / src.name)
                return _FileOutcome(src=src, size=size, error="")

            try:
                week = birdnet_week(extract_recording_time(src))
            except Exception:
                week = 1
            week_dir = dest_dir / f"week_{week:02d}"
            week_dir.mkdir(parents=True, exist_ok=True)

            if src.name.upper().endswith(".WAV"):
                try:
                    transcode_to_flac(src, week_dir / _flac_target_name(src))
                except NotLosslessError:
                    # Not 16-bit PCM: keep the original rather than degrade it.
                    shutil.copy2(src, week_dir / src.name)
            else:
                shutil.copy2(src, week_dir / src.name)

            return _FileOutcome(src=src, size=size, error="")
        except Exception as exc:  # noqa: BLE001
            return _FileOutcome(src=src, size=0, error=f"{src.name}: {exc}")
