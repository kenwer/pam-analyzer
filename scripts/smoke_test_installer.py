#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Install a release installer on a clean CI runner and check the app starts.

The app is started headless (QT_QPA_PLATFORM=offscreen) with debug logging
and counts as started once its log shows the BIRDNET_APP_DATA startup line
and the process is still alive.

Windows also runs the update as the app will: with the app still running, it
starts the installer silently with /RELAUNCH. The installer must wait for the
app to exit, remove a file planted in the install folder, and start the new
version.

Usage:
    uv run --script scripts/smoke_test_installer.py dist/PAM-Analyzer-windows-x86_64-setup.exe
"""

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APP_NAME = 'PAM Analyzer'
FILE_NAME = 'PAM-Analyzer'
STARTUP_TIMEOUT_S = 120
STAY_ALIVE_S = 10  # How long the app must stay up after its startup log line


def log_file() -> Path:
    """platformdirs.user_log_dir('PAM Analyzer', appauthor=False), spelled out to stay stdlib-only."""
    home = Path.home()
    if sys.platform == 'darwin':
        return home / 'Library' / 'Logs' / APP_NAME / 'pam-analyzer.log'
    if sys.platform == 'win32':
        return Path(os.environ['LOCALAPPDATA']) / APP_NAME / 'Logs' / 'pam-analyzer.log'
    state = os.environ.get('XDG_STATE_HOME') or str(home / '.local' / 'state')
    return Path(state) / APP_NAME / 'log' / 'pam-analyzer.log'


def run(cmd: list[str] | str) -> None:
    """Run cmd, echoing it. A string is passed to CreateProcess verbatim on Windows."""
    print(f'$ {cmd}', flush=True)
    subprocess.run(cmd, check=True)


APP_ENV = {
    **os.environ,
    'QT_QPA_PLATFORM': 'offscreen',
    'PAM_LOG_LEVEL': 'DEBUG',
    # A port nothing listens on: the update check fails fast and only logs at
    # info level, so the result never depends on GitHub or the latest release
    'PAM_UPDATE_FEED_URL': 'http://127.0.0.1:9/release.json',
}


def log_offset() -> int:
    log = log_file()
    return log.stat().st_size if log.exists() else 0


def start_app(exe: Path) -> subprocess.Popen:
    print(f'Starting {exe}', flush=True)
    # A new process group, so stopping it also stops the AppImage runtime's child
    if sys.platform == 'win32':
        return subprocess.Popen([str(exe)], env=APP_ENV, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    return subprocess.Popen([str(exe)], env=APP_ENV, start_new_session=True)


def stop_app(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        if sys.platform == 'win32':
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def wait_until_started(offset: int, proc: subprocess.Popen | None = None) -> None:
    """Wait for a BIRDNET_APP_DATA line after offset, then check the app stays up.

    proc is None for an instance this script did not start, like the one the
    installer relaunches. Its liveness then shows only through the log.
    """
    log = log_file()
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    line = None
    while line is None:
        if proc is not None and proc.poll() is not None:
            raise SystemExit(f'App exited with code {proc.returncode} before it finished starting')
        if time.monotonic() > deadline:
            raise SystemExit(f'No BIRDNET_APP_DATA line in {log} after {STARTUP_TIMEOUT_S}s')
        time.sleep(2)
        if log.exists():
            with log.open(encoding='utf-8', errors='replace') as f:
                f.seek(offset)
                line = next((ln.strip() for ln in f if 'BIRDNET_APP_DATA=' in ln), None)
    print(f'  {line}', flush=True)
    time.sleep(STAY_ALIVE_S)
    if proc is not None and proc.poll() is not None:
        raise SystemExit(f'App exited with code {proc.returncode} shortly after starting')
    print('  App is running', flush=True)


def check_log_clean(offset: int) -> None:
    log = log_file()
    if log.exists():
        with log.open(encoding='utf-8', errors='replace') as f:
            f.seek(offset)
            errors = [ln.rstrip() for ln in f if ' ERROR ' in ln or 'Traceback' in ln]
        if errors:
            raise SystemExit('The app logged errors:\n' + '\n'.join(errors))


def check_starts(exe: Path) -> None:
    """Start exe headless and wait until its log shows it started, then stop it."""
    offset = log_offset()
    proc = start_app(exe)
    try:
        wait_until_started(offset, proc)
    finally:
        stop_app(proc)
    check_log_clean(offset)


def smoke_windows(installer: Path) -> None:
    instdir = Path(os.environ['LOCALAPPDATA']) / 'Programs' / FILE_NAME
    exe = instdir / f'{FILE_NAME}.exe'
    uninstaller = instdir / 'unins000.exe'
    silent = ['/VERYSILENT', '/SUPPRESSMSGBOXES']

    run([str(installer), *silent])
    for path in (exe, uninstaller):
        if not path.is_file():
            raise SystemExit(f'Silent install did not create {path}')

    # The update as the app runs it: Setup starts while the app still runs
    offset = log_offset()
    app = start_app(exe)
    try:
        wait_until_started(offset, app)
        stale = instdir / 'stale-from-previous-version.txt'
        stale.write_text('should not survive an update')
        print('Starting the update while the app runs', flush=True)
        # The relaunched app inherits this environment, so it also runs headless
        setup = subprocess.Popen([str(installer), *silent, '/RELAUNCH'], env=APP_ENV)
        time.sleep(5)
        if setup.poll() is not None:
            raise SystemExit(f'Setup exited with code {setup.returncode} instead of waiting for the app')
        print('  Setup is waiting for the app', flush=True)
        relaunch_offset = log_offset()
    finally:
        stop_app(app)
    if setup.wait(timeout=900) != 0:
        raise SystemExit(f'Update setup exited with code {setup.returncode}')
    if stale.exists():
        raise SystemExit(f'{stale} survived the update')
    if not exe.is_file():
        raise SystemExit(f'The update did not create {exe}')
    print('Waiting for the relaunched app', flush=True)
    try:
        wait_until_started(relaunch_offset)
    finally:
        subprocess.run(['taskkill', '/IM', f'{FILE_NAME}.exe', '/F'], check=False)
    check_log_clean(offset)

    # Inno's uninstaller continues from a temporary copy, so poll for the result
    run([str(uninstaller), *silent])
    deadline = time.monotonic() + 120
    while exe.exists() and time.monotonic() < deadline:
        time.sleep(2)
    if exe.exists():
        raise SystemExit(f'Silent uninstall left {exe} behind')


def smoke_macos(dmg: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mount = Path(tmp) / 'mnt'
        mount.mkdir()
        run(['hdiutil', 'attach', '-nobrowse', '-readonly', '-mountpoint', str(mount), str(dmg)])
        try:
            link = mount / 'Applications'
            if not link.is_symlink() or os.readlink(link) != '/Applications':
                raise SystemExit('The DMG has no Applications link')
            # Stands in for the user's drag into Applications. ditto keeps the
            # bundle's symlinks and its ad-hoc signature.
            apps = Path(tmp) / 'Applications'
            apps.mkdir()
            run(['ditto', str(mount / f'{FILE_NAME}.app'), str(apps / f'{FILE_NAME}.app')])
        finally:
            run(['hdiutil', 'detach', str(mount)])
        check_starts(apps / f'{FILE_NAME}.app' / 'Contents' / 'MacOS' / FILE_NAME)


def smoke_linux(appimage: Path) -> None:
    appimage.chmod(0o755)
    check_starts(appimage)


def main() -> None:
    installer = Path(sys.argv[1]).resolve()
    if not installer.is_file():
        raise SystemExit(f'{installer} not found')
    {'win32': smoke_windows, 'darwin': smoke_macos}.get(sys.platform, smoke_linux)(installer)
    print('Smoke test passed')


if __name__ == '__main__':
    main()
