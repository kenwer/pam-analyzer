#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = ["packaging"]
# ///
"""Build PAM Analyzer distributable using PyInstaller.

Creates an isolated venv, installs the project and PyInstaller into it,
caches BirdNET model checkpoints, and runs PyInstaller.

Usage:
    uv run --script scripts/build.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

PACKAGING_DIR = Path(__file__).parent
ROOT_DIR = PACKAGING_DIR.parent

APP_ICON_PNG = ROOT_DIR / 'assets' / 'icon.png'
APP_NAME = 'pam-analyzer'
DIST_DIR = ROOT_DIR / 'dist'
BUILD_DIR = DIST_DIR / 'build' / APP_NAME
VENV_DIR = DIST_DIR / 'venv'
BIRDNET_CHECKPOINT_CACHE = DIST_DIR / '.birdnet-checkpoints'
PERCH_CHECKPOINT_CACHE = DIST_DIR / '.perch-checkpoints'

# Modules to collect via --collect-all
# QtQuick, QtQuick.Controls, QtLocation, QtPositioning are required by the MapPickerWidget
MODULES: tuple[str, ...] = (
    'PySide6.QtQuick',
    'PySide6.QtQuick.Controls',
    'PySide6.QtQuick.Window',
    'PySide6.QtLocation',
    'PySide6.QtPositioning',
)

# Extra data files to bundle via --add-data. 
# Each entry is (source, dest) where 
#  *source* is an absolute Path and 
#  *dest* is the folder inside the frozen bundle (or "." for the top-level _MEIPASS).
DATA: tuple[tuple[Path, str], ...] = (
    (ROOT_DIR / 'CHANGELOG.md', '.'),
    (ROOT_DIR / 'src' / 'pam_analyzer' / 'widgets' / 'map_picker.qml', 'widgets'),
)


def _load_dependencies() -> list[str]:
    """Load dependency list from pyproject.toml and return importable module names."""
    pyproject_path = ROOT_DIR / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
    raw_deps = data.get("project", {}).get("dependencies", [])
    return [Requirement(d).name.replace("-", "_") for d in raw_deps]

BIRDNET_PREDOWNLOAD = textwrap.dedent("""
    import os, tempfile, wave, shutil, sys
    import birdnet_analyzer
    audio_dir = tempfile.mkdtemp()
    aru_dir = os.path.join(audio_dir, 'TEST-ARU')
    os.makedirs(aru_dir)
    wav = os.path.join(aru_dir, '20240101_000000.wav')
    with wave.open(wav, 'w') as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(48000)
        wf.writeframes(bytes(48000 * 3 * 2))
    out = tempfile.mkdtemp()
    try:
        birdnet_analyzer.analyze(audio_dir, output=out, min_conf=0.99)
    except Exception as e:
        print(f'Note: {e}', file=sys.stderr)
    finally:
        shutil.rmtree(audio_dir, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)
    ckpt = os.path.join(os.path.dirname(birdnet_analyzer.__file__), 'checkpoints')
    if not os.path.isdir(ckpt) or not os.listdir(ckpt):
        print(f'ERROR: BirdNET checkpoints still missing at {ckpt}', file=sys.stderr)
        sys.exit(1)
    print(f'Checkpoints ready: {ckpt}')
""").strip()

# Pre-download Perch v2 SavedModel into birdnet_analyzer's checkpoints/perch_v2.
# ensure_perch_exists() pulls the model from kagglehub (no auth required for the
# public CPU variant) and copytrees it into cfg.PERCH_V2_MODEL_PATH. From there,
# --collect-data birdnet_analyzer bundles it into the frozen binary, so the
# shipped app never touches kagglehub at runtime.
PERCH_PREDOWNLOAD = textwrap.dedent("""
    import os, sys
    import birdnet_analyzer.config as cfg
    from birdnet_analyzer.utils import ensure_perch_exists, check_perchv2_files
    ensure_perch_exists()
    if not check_perchv2_files():
        print(f'ERROR: Perch v2 still missing at {cfg.PERCH_V2_MODEL_PATH}', file=sys.stderr)
        sys.exit(1)
    print(f'Perch v2 ready: {cfg.PERCH_V2_MODEL_PATH}')
""").strip()


def run(cmd: list, env: dict | None = None) -> None:
    subprocess.run(cmd, check=True, env=env)


def main() -> None:
    is_mac = sys.platform == 'darwin'
    is_win = sys.platform == 'win32'
    python = VENV_DIR / ('Scripts/python.exe' if is_win else 'bin/python')
    venv_env = {**os.environ, 'VIRTUAL_ENV': str(VENV_DIR)}

    print(f'Building : {APP_NAME}')
    print(f'  Platform : {sys.platform}')

    print('  Creating venv')
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    run(['uv', 'venv', '--python', '3.13', '--clear', VENV_DIR])

    print('  Installing dependencies')
    run(['uv', 'pip', 'install', '--quiet', ROOT_DIR, 'pyinstaller', '--python', python])

    # BirdNET checkpoints are not bundled with the package; they must exist before
    # PyInstaller runs so they can be included in the binary. Restored from local
    # cache when available (avoids re-downloading ~260 MB on every build).
    venv_ckpt = Path(
        subprocess.run(
            [
                python,
                '-c',
                "import birdnet_analyzer, os; print(os.path.join(os.path.dirname(birdnet_analyzer.__file__), 'checkpoints'))",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )

    if BIRDNET_CHECKPOINT_CACHE.exists() and any(BIRDNET_CHECKPOINT_CACHE.iterdir()):
        print('  Restoring BirdNET checkpoints from cache')
        shutil.copytree(BIRDNET_CHECKPOINT_CACHE, venv_ckpt, dirs_exist_ok=True)
    else:
        print('  Pre-downloading BirdNET model checkpoints')
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                run(
                    ['uv', 'run', '--no-project', 'python', '-c', BIRDNET_PREDOWNLOAD],
                    env=venv_env,
                )
                break
            except subprocess.CalledProcessError:
                if attempt == max_attempts:
                    raise
                print(f'  Download failed (attempt {attempt}/{max_attempts}), retrying...')
        shutil.copytree(venv_ckpt, BIRDNET_CHECKPOINT_CACHE, dirs_exist_ok=True)
        print(f'  Cached checkpoints to {BIRDNET_CHECKPOINT_CACHE}')

    # Perch v2 lives next to the BirdNET checkpoints inside the same package
    # dir (checkpoints/perch_v2). Restoring it after the BirdNET copytree above
    # is safe because dirs_exist_ok=True merges; the two model trees never
    # overlap in file names.
    venv_perch = venv_ckpt / 'perch_v2'
    if PERCH_CHECKPOINT_CACHE.exists() and any(PERCH_CHECKPOINT_CACHE.iterdir()):
        print('  Restoring Perch v2 checkpoints from cache')
        shutil.copytree(PERCH_CHECKPOINT_CACHE, venv_perch, dirs_exist_ok=True)
    else:
        print('  Pre-downloading Perch v2 model (~391 MB, first build only)')
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                run(
                    ['uv', 'run', '--no-project', 'python', '-c', PERCH_PREDOWNLOAD],
                    env=venv_env,
                )
                break
            except subprocess.CalledProcessError:
                if attempt == max_attempts:
                    raise
                print(f'  Download failed (attempt {attempt}/{max_attempts}), retrying...')
        shutil.copytree(venv_perch, PERCH_CHECKPOINT_CACHE, dirs_exist_ok=True)
        print(f'  Cached Perch v2 to {PERCH_CHECKPOINT_CACHE}')

    print('  Generating app icon')
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    src_png = APP_ICON_PNG
    if is_mac:
        iconset_sizes = [16, 32, 128, 256, 512]
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / 'app.iconset'
            iconset.mkdir()
            for s in iconset_sizes:
                run(['sips', '-z', str(s), str(s), src_png, '--out', str(iconset / f'icon_{s}x{s}.png')])
                run(['sips', '-z', str(s * 2), str(s * 2), src_png, '--out', str(iconset / f'icon_{s}x{s}@2x.png')])
            icon = BUILD_DIR / 'app.icns'
            run(['iconutil', '-c', 'icns', '-o', icon, iconset])
    else:
        icon = BUILD_DIR / 'app.ico'
        run(
            [
                'uv',
                'run',
                '--script',
                PACKAGING_DIR / 'png2ico.py',
                src_png,
                '-o',
                icon,
                '-s',
                '16',
                '32',
                '48',
                '64',
                '128',
                '256',
            ]
        )

    splash_png = BUILD_DIR / 'splash.png'
    if is_win:
        print('  Generating splash screen')
        run(['uv', 'run', '--script', PACKAGING_DIR / 'make_splash.py', splash_png])

    print('  Running PyInstaller')
    cmd = [
        'uv',
        'run',
        '--no-project',
        'pyinstaller',
        '--distpath',
        DIST_DIR,
        '--workpath',
        BUILD_DIR,
        '--specpath',
        BUILD_DIR,
        '--clean',
        '--noconfirm',
        '--name',
        APP_NAME,
        '--icon',
        icon,
        '--collect-data',
        'birdnet_analyzer',
    ]
    # Inject hidden imports from pyproject.toml dependencies:
    # --hidden-import <module> --hidden-import <module> ...
    for mod in _load_dependencies():
        cmd += ['--hidden-import', mod]
    # Collect modules (PySide6 QML modules, etc.) so QQuickWidget can resolve
    # the QML imports used by MapPickerWidget.
    for module in MODULES:
        cmd += ['--collect-all', module]
    # Bundle extra data files (CHANGELOG, QML, etc.).
    for src, dest in DATA:
        cmd += ['--add-data', f'{src}:{dest}']
    if is_mac:
        cmd += ['--windowed']  # creates .app bundle, no Terminal window
    else:
        cmd += ['--onefile']  # single .exe on Windows / binary on Linux
    if is_win:
        cmd += [
            '--splash',
            splash_png,
        ]  # not supported on macOS/Linux: PyInstaller's splash uses Tcl/Tk internally, which forbids secondary GUI threads on macOS
    if is_win:
        # Build as a GUI subsystem executable so Windows never allocates a
        # console window on double-click launch (avoids a console flash before
        # the runtime hook can hide it).
        cmd += ['--noconsole']
        # Runtime hook runs before any app code and handles two things:
        # - Prepends _MEIPASS to PATH so TensorFlow's self_check.py can find
        #   its DLLs via ctypes.WinDLL() (which searches %PATH%, not sys.path).
        # - Reattaches stdout/stderr to the parent console via AttachConsole(-1)
        #   so output is visible when the app is launched from a terminal
        #   (--noconsole detaches streams for double-click launches).
        cmd += ['--runtime-hook', PACKAGING_DIR / 'rthook_win_dll_path.py']
    cmd.append(ROOT_DIR / 'src' / 'pam_analyzer' / '__main__.py')
    run(cmd, env=venv_env)

    print(f'\nDone. Binary is in {DIST_DIR}/')


if __name__ == '__main__':
    main()
