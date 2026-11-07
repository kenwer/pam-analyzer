#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Install a release installer on a clean CI runner and check the app starts.

The app is started headless (QT_QPA_PLATFORM=offscreen) with debug logging
and counts as started once its log shows BIRDNET_APP_DATA pointing at the
bundled models and the process is still alive.

Windows also runs the sequence the in-app updater will use: silent uninstall
in place, then a silent install into the same folder. A file planted in the
install folder beforehand must be gone afterwards, since the installer alone
never removes files.

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
# How long the app must stay up after its log shows the models were found
STAY_ALIVE_S = 10


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


def check_starts(exe: Path) -> None:
    """Start exe headless and wait until its log shows the bundled models, then stop it."""
    log = log_file()
    offset = log.stat().st_size if log.exists() else 0
    env = {**os.environ, 'QT_QPA_PLATFORM': 'offscreen', 'PAM_LOG_LEVEL': 'DEBUG'}
    print(f'Starting {exe}', flush=True)
    # A new process group, so stopping it also stops the AppImage runtime's child
    if sys.platform == 'win32':
        proc = subprocess.Popen([str(exe)], env=env, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        proc = subprocess.Popen([str(exe)], env=env, start_new_session=True)
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        line = None
        while line is None:
            if proc.poll() is not None:
                raise SystemExit(f'App exited with code {proc.returncode} before it finished starting')
            if time.monotonic() > deadline:
                raise SystemExit(f'No BIRDNET_APP_DATA line in {log} after {STARTUP_TIMEOUT_S}s')
            time.sleep(2)
            if log.exists():
                with log.open(encoding='utf-8', errors='replace') as f:
                    f.seek(offset)
                    line = next((ln.strip() for ln in f if 'BIRDNET_APP_DATA=' in ln), None)
        print(f'  {line}', flush=True)
        if not line.replace('\\', '/').endswith('birdnet-models/birdnet-app-data'):
            raise SystemExit('The app did not point BIRDNET_APP_DATA at its bundled models')
        time.sleep(STAY_ALIVE_S)
        if proc.poll() is not None:
            raise SystemExit(f'App exited with code {proc.returncode} shortly after starting')
        print('  App is running', flush=True)
    finally:
        if proc.poll() is None:
            if sys.platform == 'win32':
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    if log.exists():
        with log.open(encoding='utf-8', errors='replace') as f:
            f.seek(offset)
            errors = [ln.rstrip() for ln in f if ' ERROR ' in ln or 'Traceback' in ln]
        if errors:
            raise SystemExit('The app logged errors:\n' + '\n'.join(errors))


def smoke_windows(installer: Path) -> None:
    instdir = Path(os.environ['LOCALAPPDATA']) / 'Programs' / APP_NAME
    exe = instdir / f'{FILE_NAME}.exe'
    uninstaller = instdir / 'uninstall.exe'

    run([str(installer), '/S'])
    for path in (exe, uninstaller):
        if not path.is_file():
            raise SystemExit(f'Silent install did not create {path}')
    check_starts(exe)

    stale = instdir / 'stale-from-previous-version.txt'
    stale.write_text('should not survive an update')
    # NSIS reads _?= and /D= only as the last argument and unquoted, so these
    # command lines are strings, which CreateProcess receives verbatim.
    # _?= makes the uninstaller run in place and block until it is done.
    run(f'"{uninstaller}" /S _?={instdir}')
    run(f'"{installer}" /S /D={instdir}')
    if stale.exists():
        raise SystemExit(f'{stale} survived the uninstall and reinstall')
    if not exe.is_file():
        raise SystemExit(f'Reinstall did not create {exe}')
    check_starts(exe)

    run(f'"{uninstaller}" /S _?={instdir}')
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
