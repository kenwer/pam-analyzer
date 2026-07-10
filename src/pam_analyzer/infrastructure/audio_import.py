"""Copies SD card files into campaign folders by BirdNET week.

WAV recordings are transcoded to FLAC on import (lossless, 16-bit PCM) to save
disk space; FLAC sources from the card and CONFIG.TXT are copied unchanged.

GUANO metadata, which lives in a WAV 'guan' RIFF chunk, is carried across the
transcode by re-embedding it in the FLAC as a 'GUANO' Vorbis comment
(see _embed_guano). Without this the timestamp, location, and device fields
would be lost on every transcoded recording.
"""

import logging
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import guano
import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

from ..domain.audio_import import (
    CardImportResult,
    ConflictChoice,
    ConflictReport,
    DetectedCard,
    FileConflict,
    ImportProgress,
    birdnet_week,
)

_log = logging.getLogger(__name__)

# Vorbis comment key under which the GUANO block is stored in a FLAC. The GUANO
# spec only defines WAV ('guan' chunk) and Anabat containers, so this name is a
# project convention; keeping it as the de-facto 'GUANO' key keeps the files
# readable by other tools that adopt the same convention.
#
# The whole serialized block goes into this one comment rather than one comment
# per GUANO field, because GUANO's text block is itself a canonical
# serialization that the guano library round-trips losslessly. Transporting it
# whole inherits that fidelity for free. Splitting it would mean re-deriving it
# on top of Vorbis comments, whose model does not match GUANO: namespaced keys
# carry '|' and spaces, field names are case-insensitive (GUANO keys are not),
# and typed values (Timestamp, Length) would need byte-identical re-assembly. A
# new vendor namespace or field then round-trips with no code change here.
_GUANO_VORBIS_KEY = "GUANO"


def read_guano(path: Path) -> guano.GuanoFile | None:
    """Return parsed GUANO metadata from a WAV or FLAC file, or None if absent.

    WAV keeps GUANO in a 'guan' RIFF chunk that the guano library reads
    directly. FLAC has no such chunk, so this importer stores the block in a
    'GUANO' Vorbis comment; for FLAC we read that comment and parse it back with
    GuanoFile.from_string. Any read or parse failure is treated as 'no metadata'
    so a malformed block never aborts an import.
    """
    try:
        if path.suffix.lower() == ".flac":
            values = FLAC(str(path)).get(_GUANO_VORBIS_KEY)
            if not values:
                return None
            return guano.GuanoFile.from_string(values[0])
        gf = guano.GuanoFile(str(path))
        return gf if gf else None
    except Exception:
        return None


def extract_recording_time(path: Path) -> datetime:
    """Return a recording timestamp for a WAV or FLAC file.

    Priority: GUANO metadata, filename YYYYMMDD_HHMMSS, file mtime.
    """
    gf = read_guano(path)
    if gf is not None:
        ts = gf.get("Timestamp")
        if ts is not None:
            return ts
    try:
        return datetime.strptime(path.stem, "%Y%m%d_%H%M%S")
    except ValueError:
        pass
    return datetime.fromtimestamp(path.stat().st_mtime)


def _embed_guano(src: Path, flac_path: Path) -> None:
    """Best-effort copy of GUANO metadata from *src* into *flac_path*.

    A no-op when the source carries no GUANO. The block is stored verbatim
    (UTF-8 text) under the 'GUANO' Vorbis comment key so the transcode does not
    discard the recording's timestamp, location, and device fields.

    Runs after the FLAC is already in place, so a write failure (e.g. a transient
    Windows file lock) is logged and swallowed rather than raised: the verified
    audio is already saved, and failing here would report the import as an error
    and keep the card. The warning keeps a systematic failure observable.
    """
    gf = read_guano(src)
    if not gf:
        return
    try:
        tags = FLAC(str(flac_path))
        tags[_GUANO_VORBIS_KEY] = gf.to_string()
        tags.save()
    except Exception:
        _log.warning("Could not embed GUANO metadata into %s", flac_path.name, exc_info=True)


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

    Any GUANO metadata on the source is re-embedded as a 'GUANO' Vorbis comment
    after the verified FLAC is moved into place (best-effort: a metadata write
    failure is logged, not raised, since the audio is already saved).

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
    _embed_guano(src, dst)
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
class DeviceProfile:
    """Where an ARU family keeps its recordings on the SD card.

    audio_subdir is the folder under the card root that holds recordings, or
    None when they sit at the card root. Sidecar files (CONFIG.TXT,
    *_Summary.txt) always live at the card root and are matched by _is_sidecar,
    so only the audio location differs between families.
    """

    audio_subdir: str | None


AUDIOMOTH_PROFILE = DeviceProfile(audio_subdir=None)
SONGMETER_PROFILE = DeviceProfile(audio_subdir="Data")


def detect_profile(card_root: Path) -> DeviceProfile:
    """Infer the ARU family from the card layout.

    A 'Data' subdirectory next to a '*_Summary.txt' marks a Song Meter card.
    Anything else is treated as AudioMoth (audio plus CONFIG.TXT at the root).
    The check is cheap: one is_dir() and one glob.
    """
    if (card_root / "Data").is_dir() and any(card_root.glob("*_Summary.txt")):
        return SONGMETER_PROFILE
    return AUDIOMOTH_PROFILE


def _is_sidecar(name: str) -> bool:
    """True for the per-device provenance file copied to the card-folder root.

    AudioMoth writes CONFIG.TXT; Song Meter writes <serial>_Summary.txt. Both
    are copied through unchanged and matched by name alone, so the per-file
    routing in detect_conflicts and _import_one needs no device profile.
    """
    upper = name.upper()
    return upper == "CONFIG.TXT" or upper.endswith("_SUMMARY.TXT")


def _audio_files_in(folder: Path) -> list[Path]:
    return [f for f in folder.iterdir() if f.is_file() and f.name.upper().endswith((".WAV", ".FLAC"))]


class AudioImporter:
    def has_direct_audio(self, card_root: Path) -> bool:
        """True if card_root holds audio or a device sidecar at its own root.

        Deliberately not recursive, unlike list_card_files: this is the
        classifier discover_folder_cards uses to tell "this is one card" apart
        from "this holds several other card folders", and list_card_files'
        extra level of date-subfolder search cannot make that distinction on
        its own, since a sibling card folder and a date subfolder both sit one
        level down and hold audio directly.
        """
        profile = detect_profile(card_root)
        audio_root = card_root / profile.audio_subdir if profile.audio_subdir else card_root
        if audio_root.is_dir() and _audio_files_in(audio_root):
            return True
        return any(_is_sidecar(f.name) for f in card_root.iterdir() if f.is_file())

    def list_card_files(self, card_root: Path) -> list[Path]:
        """Return sorted WAV/FLAC recordings plus the device sidecar from a card.

        The device profile decides where audio lives: AudioMoth keeps it at the
        card root, Song Meter under 'Data/'. Audio is searched at that location
        and, additionally, one level of subdirectories below it, to support
        devices/configurations that group recordings into per-day folders (e.g.
        card_root/20260501/*.WAV) as well as the flat layout. Sidecars
        (CONFIG.TXT, *_Summary.txt) always sit at the card root.

        The search is bounded to one extra level (not full recursion) so that
        discover_folder_cards' single-vs-batch check stays meaningful: a folder
        of several per-device card folders (each with its own date subfolders)
        must not itself look like a single card just because audio exists
        somewhere underneath it.

        WAV is transcoded to FLAC on import; FLAC already on the card is copied
        through unchanged.
        """
        profile = detect_profile(card_root)
        audio_root = card_root / profile.audio_subdir if profile.audio_subdir else card_root

        files: list[Path] = []
        if audio_root.is_dir():
            files.extend(_audio_files_in(audio_root))
            for entry in audio_root.iterdir():
                if entry.is_dir():
                    files.extend(_audio_files_in(entry))
        files.extend(f for f in card_root.iterdir() if f.is_file() and _is_sidecar(f.name))
        # Sort by filename string, not by Path: Path comparison normcases the
        # name, so bare sorted() is case-insensitive on Windows but case-
        # sensitive on POSIX. An explicit key keeps the order OS-independent.
        return sorted(files, key=lambda f: f.name)

    def detect_conflicts(self, files: list[Path], dest_dir: Path) -> ConflictReport:
        """Walk dest_dir once and classify each source as already-imported or a conflict.

        WAV sources are matched against their transcoded '.flac' target: a
        target that exists and decodes means already imported (auto-skip). A
        same-named WAV in dest (a legacy byte-copy import, or a float source
        that fell back to copy) that still byte-matches also counts as
        imported, so re-inserting a card neither re-transcodes nor leaves a
        WAV/FLAC duplicate. WAV sources never raise a user conflict because the
        import overwrites any stale target atomically.

        Passthrough sources (FLAC from the card, the device sidecar) keep their
        name and use the byte-identical check: a match is auto-skipped, a
        mismatch is a genuine conflict the user resolves.

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

            # Passthrough: FLAC from the card or the device sidecar, copied under its name.
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

        WAV is transcoded to FLAC, FLAC and CONFIG.TXT are copied unchanged.
        Files are processed one at a time. Skips are decided up front so the loop only ever does real work.
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

        for src in to_process:
            if is_cancelled():
                error = "Cancelled"
                break
            try:
                self._import_one(src, dest_dir)
            except Exception as exc:  # noqa: BLE001  one bad file must not strand the rest
                error = error or f"{src.name}: {exc}"  # report the first failure, keep going
            else:
                files_copied += 1
                bytes_copied += src.stat().st_size
                copied_sources.append(src)
            _emit()

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
            # Song Meter keeps recordings under 'Data/'; drop the directory once
            # emptied so a cleared card looks clean. rmdir only succeeds on an
            # empty dir, so a leftover (a skipped or failed file) keeps it.
            data_dir = card.mountpoint / "Data"
            if data_dir.is_dir():
                try:
                    data_dir.rmdir()
                except OSError:
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

    def _import_one(self, src: Path, dest_dir: Path) -> None:
        """Copy or transcode one source into dest_dir; raise on failure.

        WAV becomes FLAC (or a byte-copy if it is not 16-bit PCM and so has no
        lossless FLAC form); FLAC from the card and the device sidecar
        (CONFIG.TXT or <serial>_Summary.txt) are copied as-is. The caller
        catches per file, so a raised error fails only this file.
        """
        if _is_sidecar(src.name):
            shutil.copy2(src, dest_dir / src.name)
            return

        try:
            week = birdnet_week(extract_recording_time(src))
        except Exception:  # undated or unreadable timestamp: park it in week 1
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
