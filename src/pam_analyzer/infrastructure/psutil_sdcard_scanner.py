"""SD card scanner backed by psutil disk_partitions()."""

import logging
import re
import subprocess
import sys
from pathlib import Path

import psutil

from ..domain.audio_import import DetectedCard

_log = logging.getLogger(__name__)


def _get_volume_name(partition) -> str | None:
    """Return the volume label for a disk partition, or None if unavailable.

    The label is stripped of surrounding whitespace: Song Meter cards pad the
    FAT label to a fixed width ('2MM30692   '), and that label flows into a
    destination folder name, queue dedup key, and the UI. Trailing spaces in a
    directory name are rejected on Windows, so they are removed at the source.
    """
    if sys.platform == "win32":
        import ctypes

        buf = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(partition.mountpoint),
            buf,
            ctypes.sizeof(buf),
            None,
            None,
            None,
            None,
            0,
        )
        if not ok:
            return None
        return buf.value.strip() or None
    name = Path(partition.mountpoint).name.strip()
    return name or None


class PsutilSdCardScanner:
    def scan(self, name_pattern: str) -> list[DetectedCard]:
        """Return every currently-mounted card whose volume name matches name_pattern."""
        dbg = _log.isEnabledFor(logging.DEBUG)
        try:
            pattern = re.compile(name_pattern, re.IGNORECASE)
        except re.error:
            if dbg:
                _log.debug("scan: invalid pattern %r", name_pattern)
            return []

        cards: list[DetectedCard] = []
        partitions = psutil.disk_partitions()
        for partition in partitions:
            try:
                name = _get_volume_name(partition)
            except Exception:
                # A lookup failure is unusual enough (unlike a routine
                # non-match) to log even per-partition, every poll.
                if dbg:
                    _log.debug("scan: %s -> volume name lookup failed", partition.mountpoint, exc_info=True)
                continue
            if name and pattern.search(name):
                cards.append(
                    DetectedCard(
                        name=name,
                        mountpoint=Path(partition.mountpoint),
                        device=partition.device,
                    )
                )
        if dbg:
            # One summary line per poll, not one line per mounted partition:
            # this runs every 2s while watching, and most partitions never
            # come close to matching.
            _log.debug(
                "scan: pattern=%r, %d partition(s), matched: %s",
                name_pattern, len(partitions), [c.name for c in cards],
            )
        return cards

    def eject(self, card: DetectedCard) -> None:
        """Eject the given card (blocking). Raises on failure."""
        mountpoint = str(card.mountpoint)
        device = card.device
        if sys.platform == "darwin":
            subprocess.run(["diskutil", "eject", mountpoint], check=True, capture_output=True)
        elif sys.platform == "win32":
            import ctypes
            import time as _time

            drive_letter = mountpoint[0]
            FSCTL_LOCK_VOLUME = 0x00090018
            FSCTL_DISMOUNT_VOLUME = 0x00090020
            IOCTL_STORAGE_EJECT_MEDIA = 0x2D4808
            INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.CreateFileW(
                f"\\\\.\\{drive_letter}:",
                0x80000000,
                0x00000003,
                None,
                3,
                0,
                None,
            )
            if handle == INVALID_HANDLE_VALUE:
                raise OSError(f"Cannot open {drive_letter}: {ctypes.FormatError()}")
            try:
                n = ctypes.c_uint32()
                for attempt in range(1, 11):
                    if kernel32.DeviceIoControl(handle, FSCTL_LOCK_VOLUME, None, 0, None, 0, ctypes.byref(n), None):
                        break
                    err = kernel32.GetLastError()
                    if err not in (5, 32) or attempt == 10:
                        raise OSError(f"FSCTL_LOCK_VOLUME failed: {ctypes.FormatError(err)}")
                    _time.sleep(0.5)
                if not kernel32.DeviceIoControl(
                    handle, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0, ctypes.byref(n), None
                ):
                    raise OSError(f"FSCTL_DISMOUNT_VOLUME failed: {ctypes.FormatError()}")
                if not kernel32.DeviceIoControl(
                    handle, IOCTL_STORAGE_EJECT_MEDIA, None, 0, None, 0, ctypes.byref(n), None
                ):
                    raise OSError(f"IOCTL_STORAGE_EJECT_MEDIA failed: {ctypes.FormatError()}")
            finally:
                kernel32.CloseHandle(handle)
        else:
            for cmd in [
                ["udisksctl", "eject", "--block-device", device],
                ["eject", device],
                ["umount", mountpoint],
            ]:
                try:
                    subprocess.run(cmd, check=True, capture_output=True)
                    break
                except (FileNotFoundError, subprocess.CalledProcessError):
                    continue
