import math
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import guano


def toml_escape(value: str) -> str:
    """Escape a string value for inline TOML (backslashes and double quotes)."""
    return value.replace('\\', '\\\\').replace('"', '\\"')


def open_native_file_manager(path: str) -> None:
    """Open a folder in the native file manager (Finder, Explorer, Nautilus)."""
    path = os.path.normpath(path)
    try:
        if sys.platform == 'win32':
            subprocess.Popen(['explorer', path])
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])
    except Exception:
        pass


def open_file(path: str) -> None:
    """Open a file with the default application (Preview, TextEdit, etc.)."""
    path = os.path.normpath(path)
    try:
        if sys.platform == 'win32':
            os.startfile(path)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])
    except Exception:
        pass


def valid_lat(s: str) -> bool:
    """Return True if *s* is a valid latitude string (-90 to 90)."""
    try:
        return -90 <= float(s) <= 90
    except (ValueError, TypeError):
        return False


def valid_lon(s: str) -> bool:
    """Return True if *s* is a valid longitude string (-180 to 180)."""
    try:
        return -180 <= float(s) <= 180
    except (ValueError, TypeError):
        return False


def get_volume_name(partition) -> str | None:
    """Return the volume label for a disk partition, or None if unavailable."""
    if sys.platform == 'win32':
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
        return buf.value or None if ok else None
    name = Path(partition.mountpoint).name
    return name or None


def eject_sd_card(mountpoint: str, device: str) -> None:
    """Eject the volume at mountpoint (blocking)."""
    if sys.platform == 'darwin':
        subprocess.run(['diskutil', 'eject', mountpoint], check=True, capture_output=True)
    elif sys.platform == 'win32':
        import ctypes

        drive_letter = mountpoint[0]
        FSCTL_LOCK_VOLUME = 0x00090018
        FSCTL_DISMOUNT_VOLUME = 0x00090020
        IOCTL_STORAGE_EJECT_MEDIA = 0x2D4808
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateFileW(
            f'\\\\.\\{drive_letter}:',
            0x80000000,
            0x00000003,
            None,
            3,
            0,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            raise OSError(f'Cannot open {drive_letter}: {ctypes.FormatError()}')
        try:
            n = ctypes.c_uint32()
            for attempt in range(1, 11):  # retry: antivirus/indexer may briefly hold the volume
                if kernel32.DeviceIoControl(handle, FSCTL_LOCK_VOLUME, None, 0, None, 0, ctypes.byref(n), None):
                    break
                err = kernel32.GetLastError()
                if err not in (5, 32) or attempt == 10:  # 5=ACCESS_DENIED, 32=SHARING_VIOLATION
                    raise OSError(f'FSCTL_LOCK_VOLUME failed: {ctypes.FormatError(err)}')
                time.sleep(0.5)
            if not kernel32.DeviceIoControl(handle, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0, ctypes.byref(n), None):
                raise OSError(f'FSCTL_DISMOUNT_VOLUME failed: {ctypes.FormatError()}')
            if not kernel32.DeviceIoControl(
                handle,
                IOCTL_STORAGE_EJECT_MEDIA,
                None,
                0,
                None,
                0,
                ctypes.byref(n),
                None,
            ):
                raise OSError(f'IOCTL_STORAGE_EJECT_MEDIA failed: {ctypes.FormatError()}')
        finally:
            kernel32.CloseHandle(handle)
    else:  # Linux: udisksctl/eject need the device node; umount takes mountpoint
        for cmd in [
            ['udisksctl', 'eject', '--block-device', device],
            ['eject', device],
            ['umount', mountpoint],
        ]:
            try:
                subprocess.run(cmd, check=True, capture_output=True)
                break
            except (FileNotFoundError, subprocess.CalledProcessError):
                continue


def scan_sdcard_files(src_dir: Path) -> list[Path]:
    """Return sorted WAV files and CONFIG.TXT from the top level of src_dir (blocking)."""
    files = [f for f in src_dir.iterdir() if f.is_file() and ((n := f.name.upper()).endswith('.WAV') or n == 'CONFIG.TXT')]
    return sorted(files)


def birdnet_week_from_wav(path: Path) -> int:
    """Return the BirdNET week [1-48] for a WAV file (blocking).

    Determines timestamp in order: GUANO metadata, filename, file mtime.
    BirdNET week = (month - 1) * 4 + ceil(day / 7), capped at 48.
    """
    ts = None

    # Try reading timestamp from guano metadata
    try:
        ts = guano.GuanoFile(str(path)).get('Timestamp')
    except Exception:
        pass

    # Fallback 1: try reading timestamp from filename
    if ts is None:
        try:
            ts = datetime.strptime(path.stem, '%Y%m%d_%H%M%S')
        except ValueError:
            pass

    # Fallback 2: use mtime of the wav file
    if ts is None:
        ts = datetime.fromtimestamp(path.stat().st_mtime)

    return min(48, (ts.month - 1) * 4 + math.ceil(ts.day / 7))


def contract_user_path(path: str) -> str:
    """Contract the user's home directory to ~ for display purposes.

    This is the inverse of os.path.expanduser(). Useful for displaying
    paths in a more compact, user-friendly format.

    Args:
        path: A file path (absolute, relative, or with ~).

    Returns:
        The absolute path with the home directory replaced by ~, or the
        absolute path if it doesn't start with the home directory.

    Examples:
        >>> contract_user_path("/home/ken/Pictures")
        '~/Pictures'
        >>> contract_user_path("~/Documents")
        '~/Documents'
        >>> contract_user_path("Downloads")  # relative to cwd in home
        '~/Downloads'
        >>> contract_user_path("/var/log")
        '/var/log'
    """
    # Expand ~ and convert to absolute path
    home = os.path.expanduser('~')
    path = os.path.abspath(os.path.expanduser(path))

    # Check if the path starts with the home directory
    # (case-insensitive on Windows, case-sensitive otherwise)
    if sys.platform == 'win32':
        starts_with_home = path.lower().startswith(home.lower())
    else:
        starts_with_home = path.startswith(home)

    if starts_with_home:
        path = '~' + path[len(home) :]

    return path


def format_eta(done: int, total: int, elapsed: float) -> str:
    """Return an ETA string like '2m 15s', or '' if not calculable.

    Args:
        done: Number of items completed so far.
        total: Total number of items.
        elapsed: Seconds elapsed since the operation started.
    """
    if done <= 0 or done >= total or elapsed <= 0:
        return ''
    remaining_secs = int(elapsed / done * (total - done))
    mins, secs = divmod(remaining_secs, 60)
    return f'{mins}m {secs:02d}s' if mins else f'{secs}s'


def get_project_name(project_path: Path | None) -> str | None:
    """Return the project name (file stem) from a project path, or None if unavailable."""
    return project_path.stem if project_path else None


def resolve_audio_path(file_path: str, audio_root: Path | None) -> Path | None:
    """Return file_path as a path relative to audio_root, or None if unresolvable.

    Absolute paths outside audio_root and missing audio_root both return None.
    """
    if not file_path or not audio_root:
        return None
    p = Path(file_path)
    if p.is_absolute():
        try:
            return p.relative_to(audio_root)
        except ValueError:
            return None
    return p
