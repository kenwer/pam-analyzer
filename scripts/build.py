#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = []
# ///
"""Build PAM Analyzer distributable using Nuitka.

Creates an isolated venv, installs the project and Nuitka into it,
pre-downloads every model the app needs at runtime into a build-local
cache directory, then compiles the app with that cache bundled next to
the binary.

One environment variable controls where the model files land during the
download phase:

- BIRDNET_APP_DATA -> acoustic + geo weights for both shipped engines (both
  on ONNX), plus the per-locale label files. Honored by the birdnet>=1.1
  library. v3.0 downloads a vendor build. v2.4 has no upstream ONNX export
  and is converted here by convert_birdnet_2_4_onnx.py.

That directory sits inside the MODEL_CACHE root and ships as a single
--include-data-dir entry. At runtime app/__main__.py points the same env var
at the bundled location next to the binary, so the compiled app never touches
the user's home directory or the network for model loading.

Usage:
    uv run --script scripts/build.py
    uv run --script scripts/build.py --prewarm-only  # only warm MODEL_CACHE, then exit
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import tomllib
from pathlib import Path

PACKAGING_DIR = Path(__file__).parent
ROOT_DIR = PACKAGING_DIR.parent

APP_ICON_PNG = ROOT_DIR / 'assets' / 'icon.png'
APP_NAME = 'pam-analyzer'
# Matches the reverse-DNS style already used for --macos-signed-app-name.
# On Windows/Linux this also names a path component of the onefile cache
# directory (--onefile-cache-mode=cached expands {COMPANY}/{PRODUCT}/{VERSION}).
COMPANY_NAME = 'de.ken'
DIST_DIR = ROOT_DIR / 'dist'
BUILD_DIR = DIST_DIR / 'build' / APP_NAME
VENV_DIR = DIST_DIR / 'venv'

# All bundled model assets live under this single root. Reused across
# builds; delete this directory to force a fresh download.
MODEL_CACHE = DIST_DIR / '.birdnet-models'
BIRDNET_APP_DATA_CACHE = MODEL_CACHE / 'birdnet-app-data'

# Modules to pull in explicitly via --include-module, so the Qt libraries
# backing map_picker.qml ship even though no Python code imports them.
# These are the Python modules behind the QML file's own import lines
# (QtQuick, QtQuick.Controls, QtLocation, QtPositioning). The QML namespace
# names are not importable module names, and Nuitka rejects them.
MODULES: tuple[str, ...] = (
    'PySide6.QtQml',
    'PySide6.QtQuick',
    'PySide6.QtQuickControls2',
    'PySide6.QtQuickWidgets',
    'PySide6.QtLocation',
    'PySide6.QtPositioning',
)

# Extra data files to bundle via --include-data-files.
# Each entry is (source, dest) where *source* is an absolute Path and *dest*
# is the file's path inside the output tree, relative to the binary.
# The files use the real package paths because each is read through a
# package-relative lookup at runtime, which resolves the same way in
# a compiled build as in a source run.
DATA: tuple[tuple[Path, str], ...] = (
    (
        ROOT_DIR / 'src' / 'pam_analyzer' / 'widgets' / 'map_picker.qml',
        'pam_analyzer/widgets/map_picker.qml',
    ),
    # species_names.py reads this via importlib.resources.files(__package__).
    (
        ROOT_DIR / 'src' / 'pam_analyzer' / 'infrastructure' / 'data' / 'species_aliases.tsv',
        'pam_analyzer/infrastructure/data/species_aliases.tsv',
    ),
    # perch_onnx.labels() reads this the same way. REQUIRED_MODEL_FILES checks
    # the model cache, a different tree, so nothing else catches its absence.
    (
        ROOT_DIR / 'src' / 'pam_analyzer' / 'infrastructure' / 'data' / 'perch_v2_labels.csv',
        'pam_analyzer/infrastructure/data/perch_v2_labels.csv',
    ),
)


def _app_version() -> str:
    """Read the project version from pyproject.toml, for the bundle metadata."""
    with open(ROOT_DIR / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    return data["project"]["version"]


# Single prewarm script: load every model the app reaches for, so the
# downloads happen here (with retry-on-failure) rather than on first user
# click. BIRDNET_APP_DATA points at the build cache, so files land in a known
# location ready for PyInstaller bundling. The precision must match
# birdnet_lib._PRECISION or the bundle holds weights the app never asks for.
MODEL_PREWARM = textwrap.dedent("""
    import sys
    import birdnet
    from birdnet.acoustic.models.v2_4.model import AcousticDownloaderBaseV2_4
    from birdnet.acoustic.models.v3_0.model import AcousticDownloaderBaseV3_0
    from birdnet.geo.models.v2_4.model import GeoDownloaderBaseV2_4
    from birdnet.geo.models.v3_0.model import GeoDownloaderBaseV3_0
    from birdnet.utils.local_data import get_lang_dir

    # Only v3.0 downloads a vendor build. The v2.4 pair has no upstream ONNX
    # export and is produced by convert_birdnet_2_4_onnx.py, which runs before
    # this script and writes into the same cache.
    for kind, version, backend, kwargs in (
        ('acoustic', '3.0', 'onnx', {}),
        ('geo', '3.0', 'onnx', {}),
    ):
        print(f'Pre-downloading birdnet {kind} v{version} {backend} (en_us)...', file=sys.stderr)
        birdnet.load(kind, version, backend, lang='en_us', precision='fp32', **kwargs)

    # Loading one locale generates the label files for every locale, so the
    # bundled app can switch species language offline. Asserted against the
    # lib's own language set because a partial label set is invisible here
    # and only surfaces as a download on an end user's machine. The two
    # engines ship different language sets, so each is checked against its
    # own downloader.
    for kind, version, backend, downloader in (
        ('acoustic', '3.0', 'onnx', AcousticDownloaderBaseV3_0),
        ('geo', '3.0', 'onnx', GeoDownloaderBaseV3_0),
        ('acoustic', '2.4', 'onnx', AcousticDownloaderBaseV2_4),
        ('geo', '2.4', 'onnx', GeoDownloaderBaseV2_4),
    ):
        lang_dir = get_lang_dir(kind, version, backend)
        missing = sorted(
            lang for lang in downloader.AVAILABLE_LANGUAGES
            if not (lang_dir / (lang + '.txt')).is_file()
        )
        if missing:
            raise SystemExit(f'{kind} v{version} labels missing for: {missing}')
    print('All models cached.')
""").strip()


def run(cmd: list, env: dict | None = None) -> None:
    subprocess.run(cmd, check=True, env=env)


def _convert_v2_4_models(download_env: dict) -> None:
    """Produce the v2.4 ONNX weights, which no upstream release ships.

    Runs before MODEL_PREWARM because the prewarm script asserts the v2.4
    label files exist, and this is what installs them.

    Invoked with `uv run --script` so it resolves the PEP 723 header at the top
    of the conversion script: TensorFlow and tf2onnx on their own interpreter,
    reachable from neither the project venv nor the build venv. Being a
    separate environment is the point, since the app must never install
    TensorFlow.

    No retry loop wraps this. The script skips work when its output is already
    current, so a failure here is a real conversion failure rather than a
    flaky download, and the download it does make is retried inside birdnet.
    """
    print('  Converting BirdNET v2.4 to onnx (downloads the SavedModel on a cold cache)')
    run(
        ['uv', 'run', '--script', str(PACKAGING_DIR / 'convert_birdnet_2_4_onnx.py')],
        env=download_env,
    )


def _fetch_perch_model(download_env: dict) -> None:
    """Fetch the Perch v2 ONNX weights, which no upstream release ships either.

    Unlike v2.4 this cannot be converted here. Perch's SavedModel is a jax2tf
    export whose graph sits inside XlaCallModule ops, which tf2onnx cannot walk,
    so the app depends on a published third-party export pinned by commit and
    checksum. The script verifies before installing and skips when the file is
    already current.
    """
    print('  Fetching Perch v2 onnx (413 MB on a cold cache)')
    run(
        ['uv', 'run', '--script', str(PACKAGING_DIR / 'fetch_perch_onnx.py')],
        env=download_env,
    )


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

    _convert_v2_4_models(download_env)
    _fetch_perch_model(download_env)

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
    'acoustic-models/v2.4/onnx/model-fp32.onnx',
    'geo-models/v2.4/onnx/model-fp32.onnx',
    # Perch has no geo model of its own and reuses v3.0's for the allow-list.
    'acoustic-models/perch-v2/onnx/model-fp32.onnx',
)


def _verify_bundle(app_root: Path) -> None:
    """Fail the build if the bundled data did not make it into the output.

    A compiled app whose models are missing still starts and still runs: it
    just re-downloads model files on the user's first Analyze click, with the
    lib's progress bar going to a stderr that a GUI user won't see. So it is
    checked here.

    app_root is a directory tree with every file present: the macOS .app, or,
    on Windows/Linux, the standalone .dist tree Nuitka builds on its way to
    the single-file executable. The onefile binary itself seals its payload
    and cannot be stat'd, so --remove-output is skipped on those platforms
    and the caller checks the .dist tree before deleting it.
    """
    expected = {
        rel: (BIRDNET_APP_DATA_CACHE / rel).stat().st_size for rel in REQUIRED_MODEL_FILES
    }
    payload = sum(expected.values())

    for rel, size in expected.items():
        found = list(app_root.glob(f'**/birdnet-models/birdnet-app-data/{rel}'))
        if not found:
            raise SystemExit(
                f'{rel} is missing from the bundle. The model tree did not survive '
                'Nuitka. Check the --include-data-dir entry for MODEL_CACHE.'
            )
        actual = found[0].stat().st_size
        if actual != size:
            raise SystemExit(
                f'{rel} is {actual} bytes in the bundle, expected {size}. '
                'The bundled copy is truncated or stale.'
            )

    for _src, dest in DATA:
        if not list(app_root.glob(f'**/{dest}')):
            raise SystemExit(
                f'{dest} is missing from the bundle. Check its --include-data-files entry.'
            )

    print(f'  Verified bundle: {payload / 1e6:.0f} MB of models, {len(DATA)} data files')


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
    run(['uv', 'venv', '--python', '3.14', '--clear', VENV_DIR])

    print('  Syncing build venv (runtime deps + nuitka)')
    # The app is never installed. Nuitka compiles it straight from src/, which
    # is also the only way the gitignored ui_*.py and *_rc.py modules reach the
    # build: a wheel would omit them, since hatchling selects files via git.
    # nuitka comes from the build group so uv.lock pins it.
    # --no-default-groups drops dev, which the build does not use: the compile
    # scripts call pyside6-uic and pyside6-rcc, both shipped with PySide6.
    run(
        [
            'uv', 'sync',
            '--python', str(python),
            '--no-install-project',
            '--no-default-groups',
            '--group', 'build',
        ],
        env=build_env,
    )

    print('  Compiling Qt resources and UI files')
    # .ui -> ui_*.py and .qrc -> *_rc.py must exist in src/ before Nuitka reads
    # that tree, since it compiles the sources rather than an installed copy.
    run(['uv', 'run', '--no-project', 'python', str(PACKAGING_DIR / 'compile_ui.py')], env=build_env)
    run(['uv', 'run', '--no-project', 'python', str(PACKAGING_DIR / 'compile_qrc.py')], env=build_env)

    download_env = {
        **build_env,
        'BIRDNET_APP_DATA': str(BIRDNET_APP_DATA_CACHE),
    }
    _prewarm_models(download_env, ['uv', 'run', '--no-project'])

    print('  Generating app icon')
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    src_png = APP_ICON_PNG
    icon = src_png  # Nuitka takes a PNG directly on Linux
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
    elif is_win:
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

    print('  Running Nuitka')
    # Remove any output from previous builds
    for pattern in ('*.dist', '*.app', '*.build'):
        for stale in BUILD_DIR.glob(pattern):
            shutil.rmtree(stale)
    binary_name = f'{APP_NAME}.exe' if is_win else APP_NAME
    cmd = [
        'uv',
        'run',
        '--no-project',
        'python',
        '-m',
        'nuitka',
        '--standalone',
        '--enable-plugin=pyside6',
        '--include-qt-plugins=platforminputcontexts,geoservices,multimedia,position,qml',
        '--noinclude-qt-translations',
        '--noinclude-dlls=*.cpp.o',
        '--noinclude-dlls=*.qsb',
        # birdnet imports ai_edge_litert inside load_lib_litert_model to run
        # TFLite weights, a backend this app never selects. The [tool.uv]
        # override in pyproject.toml already keeps it out of the venv. Saying
        # so here too means dropping that override cannot silently add 32 MB.
        '--nofollow-import-to=ai_edge_litert',
        # birdnet resolves backends by name at runtime, so following its
        # imports statically does not reach every module it loads.
        '--include-package=birdnet',
        '--include-package-data=birdnet',
        '--assume-yes-for-downloads',
        # Compiling the package directory rather than __main__.py keeps the
        # package context, so the relative imports inside the package resolve
        # as they do under `python -m pam_analyzer`.
        '--python-flag=-m',
        f'--output-dir={BUILD_DIR}',
        f'--output-filename={binary_name}',
    ]
    cmd += [f'--include-module={module}' for module in MODULES]
    # The model tree lands next to the binary, where app/__main__.py points
    # BIRDNET_APP_DATA at startup.
    cmd.append(f'--include-data-dir={MODEL_CACHE}=birdnet-models')
    cmd += [f'--include-data-files={src}={dest}' for src, dest in DATA]
    if is_mac:
        # Standalone .app, not onefile
        cmd += [
            '--remove-output',
            '--macos-create-app-bundle',
            f'--macos-app-icon={icon}',
            f'--macos-app-name={APP_NAME}',
            f'--macos-app-version={_app_version()}',
            f'--macos-signed-app-name={COMPANY_NAME}.{APP_NAME}',
        ]
    else:
        # onefile-cache-mode=cached unpacks the app into
        # {CACHE_DIR}/{COMPANY}/{PRODUCT}/{VERSION} and reuses it on later
        # launches.
        # --company-name/--product-name/--product-version are what fill in
        # those placeholders, so the cache path only depends on the version.
        # --remove-output is skipped here: the onefile binary seals its
        # payload, so _verify_bundle checks the intermediate .dist tree
        # before main() deletes it.
        cmd += [
            '--onefile',
            '--onefile-cache-mode=cached',
            f'--company-name={COMPANY_NAME}',
            f'--product-name={APP_NAME}',
            f'--product-version={_app_version()}',
        ]
        if is_win:
            cmd += [
                f'--windows-icon-from-ico={icon}',
                '--windows-console-mode=attach',  # Reuses parent console on terminal launch, no console on double-click
                '--msvc=latest',
                f'--onefile-windows-splash-screen-image={splash_png}',
            ]
        else:
            cmd.append(f'--linux-icon={icon}')
    cmd.append(ROOT_DIR / 'src' / 'pam_analyzer')
    run(cmd, env=build_env)

    print('  Moving output into place')
    if is_mac:
        # Nuitka names the tree after the compiled package, so it arrives as
        # pam_analyzer.app. Globbing rather than hardcoding that name keeps
        # the rename working if Nuitka changes how it derives it.
        produced = list(BUILD_DIR.glob('*.app'))
        if len(produced) != 1:
            raise SystemExit(f'Expected exactly one *.app in {BUILD_DIR}, found {produced}')
        final = DIST_DIR / f'{APP_NAME}.app'
        if final.exists():
            shutil.rmtree(final)
        shutil.move(str(produced[0]), str(final))

        print('  Verifying bundled models')
        _verify_bundle(final)
    else:
        # --remove-output was skipped for onefile, so the standalone .dist
        # tree Nuitka built on its way to the single-file binary is still
        # here. Verify the exact per-file bundle against it, the same check
        # macOS gets, then discard it: only the sealed onefile binary ships.
        dist_trees = list(BUILD_DIR.glob('*.dist'))
        if len(dist_trees) != 1:
            raise SystemExit(f'Expected exactly one *.dist in {BUILD_DIR}, found {dist_trees}')
        print('  Verifying bundled models')
        _verify_bundle(dist_trees[0])
        shutil.rmtree(dist_trees[0])
        build_trees = list(BUILD_DIR.glob('*.build'))
        for stale in build_trees:
            shutil.rmtree(stale)

        produced_file = BUILD_DIR / binary_name
        if not produced_file.is_file():
            raise SystemExit(f'Expected {binary_name} in {BUILD_DIR}, not found')
        final = DIST_DIR / binary_name
        if final.exists():
            final.unlink()
        shutil.move(str(produced_file), str(final))

    print(f'\nDone. Output is in {final}')


if __name__ == '__main__':
    main()
