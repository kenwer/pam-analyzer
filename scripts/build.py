#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = ["packaging"]
# ///
"""Build PAM Analyzer distributable using PyInstaller.

Creates an isolated venv, installs the project and PyInstaller into it,
pre-downloads every model the app needs at runtime into a build-local
cache directory, then runs PyInstaller with that cache bundled into the
binary.

One environment variable controls where the model files land during the
download phase:

- BIRDNET_APP_DATA -> acoustic v3.0 + geo v3.0 ONNX weights, plus the
  per-locale label files. Honored by the birdnet>=1.1 library.

That directory sits inside the MODEL_CACHE root and ships as a single
--add-data entry. At runtime app/__main__.py points the same env var at the
bundled location inside _MEIPASS, so the frozen app never touches the user's
home directory or the network for model loading.

Usage:
    uv run --script scripts/build.py
    uv run --script scripts/build.py --prewarm-only  # only warm MODEL_CACHE, then exit
"""

import argparse
import os
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

# All bundled model assets live under this single root. Reused across
# builds; delete this directory to force a fresh download.
MODEL_CACHE = DIST_DIR / '.birdnet-models'
BIRDNET_APP_DATA_CACHE = MODEL_CACHE / 'birdnet-app-data'

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
    # legacy_names.py reads this via importlib.resources.files(__package__),
    # so it must land at its real package path, not a bespoke top-level folder.
    (
        ROOT_DIR / 'src' / 'pam_analyzer' / 'infrastructure' / 'data' / 'legacy_species_aliases.tsv',
        'pam_analyzer/infrastructure/data',
    ),
)


def _load_dependencies() -> list[str]:
    """Load dependency list from pyproject.toml and return importable module names."""
    pyproject_path = ROOT_DIR / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
    raw_deps = data.get("project", {}).get("dependencies", [])
    return [Requirement(d).name.replace("-", "_") for d in raw_deps]


# Single prewarm script: load every model the app reaches for, so the
# downloads happen here (with retry-on-failure) rather than on first user
# click. BIRDNET_APP_DATA points at the build cache, so files land in a known
# location ready for PyInstaller bundling. The precision must match
# birdnet_lib._PRECISION or the bundle holds weights the app never asks for.
MODEL_PREWARM = textwrap.dedent("""
    import sys
    import birdnet
    from birdnet.acoustic.models.v3_0.model import AcousticDownloaderBaseV3_0
    from birdnet.geo.models.v3_0.model import GeoDownloaderBaseV3_0
    from birdnet.utils.local_data import get_lang_dir

    print('Pre-downloading birdnet acoustic v3.0 onnx (en_us)...', file=sys.stderr)
    birdnet.load('acoustic', '3.0', 'onnx', lang='en_us', precision='fp32')
    print('Pre-downloading birdnet geo v3.0 onnx (en_us)...', file=sys.stderr)
    birdnet.load('geo', '3.0', 'onnx', lang='en_us', precision='fp32')

    # Loading one locale generates the label files for every locale, so the
    # bundled app can switch species language offline. Asserted against the
    # lib's own language set because a partial label set is invisible here
    # and only surfaces as a download on an end user's machine.
    for kind, downloader in (
        ('acoustic', AcousticDownloaderBaseV3_0),
        ('geo', GeoDownloaderBaseV3_0),
    ):
        lang_dir = get_lang_dir(kind, '3.0', 'onnx')
        missing = sorted(
            lang for lang in downloader.AVAILABLE_LANGUAGES
            if not (lang_dir / (lang + '.txt')).is_file()
        )
        if missing:
            raise SystemExit(f'{kind} v3.0 labels missing for: {missing}')
    print('All models cached.')
""").strip()


def run(cmd: list, env: dict | None = None) -> None:
    subprocess.run(cmd, check=True, env=env)


def _prewarm_models(download_env: dict, uv_run_prefix: list) -> None:
    """Download every model into MODEL_CACHE, with retry on transient failures.

    Always runs the prewarm script. birdnet decides freshness offline, from
    the byte size and sha256 constants the installed version pins, so a warm
    run finishes in about a second and a model whose pinned release changed
    is re-fetched. A coarse "cache directory is non-empty" skip used to live
    here, but it could ship a stale bundle.

    uv_run_prefix selects which venv runs the download: the isolated build
    venv (['uv', 'run', '--no-project'], with VIRTUAL_ENV/UV_PROJECT_ENVIRONMENT
    set in download_env) for packaging, or the regular project venv
    (['uv', 'run']) for CI's --prewarm-only, which only needs the models on
    disk before tests run and has no isolated venv to point at.
    """
    BIRDNET_APP_DATA_CACHE.mkdir(parents=True, exist_ok=True)

    print('  Pre-downloading model checkpoints (BirdNET acoustic + geo, v3.0 onnx)')
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            run(
                [*uv_run_prefix, 'python', '-c', MODEL_PREWARM],
                env=download_env,
            )
            break
        except subprocess.CalledProcessError:
            if attempt == max_attempts:
                raise
            print(f'  Download failed (attempt {attempt}/{max_attempts}), retrying...')
    print(f'  Cached models to {MODEL_CACHE}')


# Model files that must survive into the finished bundle, relative to
# BIRDNET_APP_DATA_CACHE. The per-locale label files are checked by the
# prewarm script instead, which can compare them against the lib's own
# language set.
REQUIRED_MODEL_FILES: tuple[str, ...] = (
    'acoustic-models/v3.0/onnx/model-fp32.onnx',
    'geo-models/v3.0/onnx/model-fp32.onnx',
)


def _verify_bundle(is_onefile: bool) -> None:
    """Fail the build if the model tree did not make it into the output.

    A frozen app whose models are missing still starts and still runs: it
    just re-downloads ~557 MB on the user's first Analyze click, with the
    lib's progress bar going to a stderr that a windowed build sends to
    devnull. That failure has shipped before, from a malformed --add-data
    separator, and it is invisible from the build log. So it is checked here.

    The two output shapes need different checks. An onedir build (the macOS
    .app) keeps the tree on disk, so the files are located and their sizes
    compared exactly. A onefile build seals its payload inside the
    executable, where the files cannot be stat'd without unpacking, so the
    binary is instead required to be larger than the models it should carry.
    The threshold is deliberately loose, since it only has to separate a
    bundle holding the weights from one that dropped them.
    """
    expected = {
        rel: (BIRDNET_APP_DATA_CACHE / rel).stat().st_size for rel in REQUIRED_MODEL_FILES
    }
    payload = sum(expected.values())

    if is_onefile:
        binaries = [
            f for f in DIST_DIR.iterdir() if f.is_file() and f.stem == APP_NAME
        ]
        if not binaries:
            raise SystemExit(f'No {APP_NAME} binary found in {DIST_DIR}')
        size = max(f.stat().st_size for f in binaries)
        floor = int(payload * 0.6)
        if size < floor:
            raise SystemExit(
                f'The built binary is {size / 1e6:.0f} MB, below the {floor / 1e6:.0f} MB '
                f'floor for a bundle carrying {payload / 1e6:.0f} MB of models. '
                'The model tree is probably missing from --add-data.'
            )
        print(f'  Verified bundle: {size / 1e6:.0f} MB, carries the model payload')
        return

    for rel, size in expected.items():
        found = list(DIST_DIR.glob(f'**/birdnet-models/birdnet-app-data/{rel}'))
        if not found:
            raise SystemExit(
                f'{rel} is missing from the bundle. The model tree did not survive '
                'PyInstaller; check the --add-data entry for MODEL_CACHE.'
            )
        actual = found[0].stat().st_size
        if actual != size:
            raise SystemExit(
                f'{rel} is {actual} bytes in the bundle, expected {size}. '
                'The bundled copy is truncated or stale.'
            )
    print(f'  Verified bundle: {payload / 1e6:.0f} MB of models present')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--prewarm-only',
        action='store_true',
        help=(
            'Only pre-download models into MODEL_CACHE using the project venv, then exit. '
            'Used by CI to warm the cache before the test step runs, so slow model-loading '
            'tests never trigger a bare, un-retried download.'
        ),
    )
    args = parser.parse_args()

    if args.prewarm_only:
        DIST_DIR.mkdir(parents=True, exist_ok=True)
        download_env = {
            **os.environ,
            'BIRDNET_APP_DATA': str(BIRDNET_APP_DATA_CACHE),
        }
        _prewarm_models(download_env, ['uv', 'run'])
        return

    is_mac = sys.platform == 'darwin'
    is_win = sys.platform == 'win32'
    python = VENV_DIR / ('Scripts/python.exe' if is_win else 'bin/python')

    # Force uv to use our build venv instead of the default project .venv.
    # VIRTUAL_ENV is for general python tool awareness, UV_PROJECT_ENVIRONMENT
    # is specifically to stop uv from auto-discovering the root .venv.
    build_env = {
        **os.environ,
        'VIRTUAL_ENV': str(VENV_DIR),
        'UV_PROJECT_ENVIRONMENT': str(VENV_DIR),
    }

    print(f'Building : {APP_NAME}')
    print(f'  Platform : {sys.platform}')

    print('  Creating venv')
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    run(['uv', 'venv', '--python', '3.13', '--clear', VENV_DIR])

    print('  Syncing build venv (project + dev deps)')
    # uv sync installs the project + all dependency groups.
    # --no-install-project skips editable install so we can compile .ui/.qrc
    # before the package is installed (compiled files are picked up by pip install).
    run(
        ['uv', 'sync', '--python', str(python), '--group', 'dev', '--no-install-project'],
        env=build_env,
    )

    print('  Compiling Qt resources and UI files (before install)')
    # Compile .ui -> ui_*.py and .qrc -> *_rc.py before installing the package
    # so that `uv pip install` picks them up as part of the source tree.
    run(['uv', 'run', '--no-project', 'python', str(PACKAGING_DIR / 'compile_ui.py')], env=build_env)
    run(['uv', 'run', '--no-project', 'python', str(PACKAGING_DIR / 'compile_qrc.py')], env=build_env)

    print('  Installing project + pyinstaller')
    run(['uv', 'pip', 'install', '--quiet', ROOT_DIR, 'pyinstaller', '--python', python], env=build_env)

    download_env = {
        **build_env,
        'BIRDNET_APP_DATA': str(BIRDNET_APP_DATA_CACHE),
    }
    _prewarm_models(download_env, ['uv', 'run', '--no-project'])

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
        # BIRDNET_APP_DATA; that tree is added via --add-data below.
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
    # BIRDNET_APP_DATA at a subdir of that path.
    # PyInstaller splits --add-data on os.pathsep, which is ';' on Windows
    # and ':' on POSIX. A hardcoded ':' is malformed on Windows (the source
    # path also starts with a drive-letter colon), so the tree never lands
    # in the bundle and the frozen app re-downloads every model at runtime.
    cmd += ['--add-data', f'{MODEL_CACHE}{os.pathsep}birdnet-models']
    # Bundle extra data files (CHANGELOG, QML, etc.).
    for src, dest in DATA:
        cmd += ['--add-data', f'{src}{os.pathsep}{dest}']
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
    run(cmd, env=build_env)

    print('  Verifying bundled models')
    _verify_bundle(is_onefile=not is_mac)

    print(f'\nDone. Binary is in {DIST_DIR}/')


if __name__ == '__main__':
    main()
