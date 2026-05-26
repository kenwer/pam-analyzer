#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["packaging"]
# ///
"""Build PAM Analyzer distributable using PyInstaller.

Creates an isolated venv, installs the project and PyInstaller into it,
pre-downloads every model the app needs at runtime into a build-local
cache directory, then runs PyInstaller with that cache bundled into the
binary.

Two environment variables control where the model files land during the
download phase:

- BIRDNET_APP_DATA -> acoustic v2.4 + geo v2.4 (model weights + per-locale
  label .txt files). Honored by the birdnet>=0.2 library.
- KAGGLEHUB_CACHE -> Perch v2 SavedModel. Honored by kagglehub, which the
  birdnet library uses internally to fetch the Perch checkpoint.

Both directories are placed inside one MODEL_CACHE root and shipped as a
single --add-data entry. At runtime app/__main__.py points the same two
env vars at the bundled location inside _MEIPASS, so the frozen app never
touches the user's home directory or the network for model loading.

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

# All bundled model assets live under this single root, with one subdir
# per env-var-driven cache (one for the birdnet lib, one for kagglehub).
# Reused across builds; delete this directory to force a fresh download.
MODEL_CACHE = DIST_DIR / '.birdnet-models'
BIRDNET_APP_DATA_CACHE = MODEL_CACHE / 'birdnet-app-data'
KAGGLEHUB_CACHE = MODEL_CACHE / 'kagglehub'

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


# Single prewarm script: load every model the app might reach for, so the
# downloads happen here (with retry-on-failure) rather than on first user
# click. BIRDNET_APP_DATA / KAGGLEHUB_CACHE point at the build cache, so
# files land in a known location ready for PyInstaller bundling.
MODEL_PREWARM = textwrap.dedent("""
    import sys
    import birdnet
    print('Pre-downloading birdnet acoustic v2.4 (en_us)...', file=sys.stderr)
    birdnet.load('acoustic', '2.4', 'tf', lang='en_us')
    print('Pre-downloading birdnet geo v2.4 (en_us)...', file=sys.stderr)
    birdnet.load('geo', '2.4', 'tf', lang='en_us')
    print('Pre-downloading Perch v2 (CPU)...', file=sys.stderr)
    birdnet.load_perch_v2(device='CPU')
    print('All models cached.')
""").strip()


def run(cmd: list, env: dict | None = None) -> None:
    subprocess.run(cmd, check=True, env=env)


def _prewarm_models(python: Path, venv_env: dict) -> None:
    """Download every model into MODEL_CACHE, with retry on transient failures.

    Skips the download when the cache already exists and is non-empty,
    so repeat builds reuse the previous download. Delete MODEL_CACHE to
    force a fresh fetch.
    """
    if MODEL_CACHE.exists() and any(MODEL_CACHE.rglob('*')):
        # Sanity check: the two subdirs the lib expects should both
        # contain files. If either is empty, fall through to a refetch.
        a_ok = BIRDNET_APP_DATA_CACHE.exists() and any(BIRDNET_APP_DATA_CACHE.rglob('*'))
        k_ok = KAGGLEHUB_CACHE.exists() and any(KAGGLEHUB_CACHE.rglob('*'))
        if a_ok and k_ok:
            print(f'  Using cached models at {MODEL_CACHE}')
            return

    BIRDNET_APP_DATA_CACHE.mkdir(parents=True, exist_ok=True)
    KAGGLEHUB_CACHE.mkdir(parents=True, exist_ok=True)

    download_env = {
        **venv_env,
        'BIRDNET_APP_DATA': str(BIRDNET_APP_DATA_CACHE),
        'KAGGLEHUB_CACHE': str(KAGGLEHUB_CACHE),
    }

    print('  Pre-downloading model checkpoints (BirdNET acoustic + geo, Perch v2)')
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            run(
                ['uv', 'run', '--no-project', 'python', '-c', MODEL_PREWARM],
                env=download_env,
            )
            break
        except subprocess.CalledProcessError:
            if attempt == max_attempts:
                raise
            print(f'  Download failed (attempt {attempt}/{max_attempts}), retrying...')
    print(f'  Cached models to {MODEL_CACHE}')


def main() -> None:
    is_mac = sys.platform == 'darwin'
    is_win = sys.platform == 'win32'
    python = VENV_DIR / ('Scripts/python.exe' if is_win else 'bin/python')
    venv_env = {**os.environ, 'VIRTUAL_ENV': str(VENV_DIR)}

    print(f'Building : {APP_NAME}')
    print(f'  Platform : {sys.platform}')

    print('  Creating venv')
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    run(['uv', 'venv', '--python', '3.12', '--clear', VENV_DIR])

    print('  Installing dependencies')
    run(['uv', 'pip', 'install', '--quiet', ROOT_DIR, 'pyinstaller', '--python', python])

    _prewarm_models(python, venv_env)

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
        # Include any non-Python data the birdnet package itself ships.
        # Model weights and labels live outside the package under
        # BIRDNET_APP_DATA / KAGGLEHUB_CACHE; those are added via DATA below.
        '--collect-data',
        'birdnet',
    ]
    # Inject hidden imports from pyproject.toml dependencies:
    # --hidden-import <module> --hidden-import <module> ...
    for mod in _load_dependencies():
        cmd += ['--hidden-import', mod]
    # Collect modules (PySide6 QML modules, etc.) so QQuickWidget can resolve
    # the QML imports used by MapPickerWidget.
    for module in MODULES:
        cmd += ['--collect-all', module]
    # Bundle the model cache as a single tree at <bundle>/birdnet-models.
    # app/__main__.py reads sys._MEIPASS at startup and points
    # BIRDNET_APP_DATA / KAGGLEHUB_CACHE at the subdirs of that path.
    cmd += ['--add-data', f'{MODEL_CACHE}:birdnet-models']
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
