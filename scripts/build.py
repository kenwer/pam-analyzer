#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = []
# ///
"""Build the PAM Analyzer installer using Nuitka.

Creates an isolated venv, installs the project and Nuitka into it,
pre-downloads every model the app needs at runtime into a build-local
cache directory, then compiles the app with that cache bundled next to
the binary. Nuitka wraps the result in the platform's installer: an NSIS
setup.exe on Windows, a DMG on macOS, an AppImage on Linux. The installer
lands in dist/ under the release asset name the in-app updater looks for.

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
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request
from pathlib import Path

PACKAGING_DIR = Path(__file__).parent
ROOT_DIR = PACKAGING_DIR.parent

APP_ICON_PNG = ROOT_DIR / 'assets' / 'icon.png'
# Shown in the Dock, the Start menu and Apps & Features.
APP_NAME = 'PAM Analyzer'
# The binary, the .app/.dist folder and the release assets. Unversioned, so
# Dock items and shortcuts survive an update.
FILE_NAME = 'PAM-Analyzer'
COMPANY_NAME = 'de.ken'
# Unchanged from the onefile builds. macOS keys folder permissions to it.
BUNDLE_ID = f'{COMPANY_NAME}.pam-analyzer'
DIST_DIR = ROOT_DIR / 'dist'
BUILD_DIR = DIST_DIR / 'build' / FILE_NAME
VENV_DIR = DIST_DIR / 'venv'

# The maintained appimagetool embeds the static type2 runtime, so the AppImage
# runs without libfuse2. Nuitka's default download is the older AppImageKit one.
APPIMAGETOOL_VERSION = '1.9.1'
APPIMAGETOOL_SHA256 = {
    'x86_64': 'ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0',
    'aarch64': 'f0837e7448a0c1e4e650a93bb3e85802546e60654ef287576f46c71c126a9158',
}

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


def _app_version(python: Path) -> str:
    """The version installed into the build venv, which hatch-vcs takes from git, like 0.7.6.dev2."""
    result = subprocess.run(
        [str(python), '-c', 'from importlib.metadata import version; print(version("pam-analyzer"))'],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _numeric_versions(version: str) -> tuple[str, str]:
    """
    The app version as the plain numbers bundle metadata takes: 0.7.6 for the
    macOS bundle, and 0.7.6.2 for --product-version, where the dev number
    tells dev builds apart in the Windows version resource.
    """
    match = re.fullmatch(r'(\d+\.\d+\.\d+)(?:\.dev(\d+))?', version)
    if match is None:
        raise SystemExit(f'Unexpected app version {version!r}. Is the checkout missing its git tags?')
    release, dev = match.groups()
    return release, f'{release}.{dev}' if dev else release


def _asset_name() -> str:
    """The installer's release asset name, like PAM-Analyzer-windows-x86_64-setup.exe.

    The in-app updater picks the asset for its platform by this exact name.
    """
    arch = {'amd64': 'x86_64', 'x86_64': 'x86_64', 'arm64': 'arm64', 'aarch64': 'arm64'}.get(
        platform.machine().lower()
    )
    if arch is None:
        raise SystemExit(f'Unsupported architecture {platform.machine()!r}')
    if sys.platform == 'darwin':
        return f'{FILE_NAME}-macos-{arch}.dmg'
    if sys.platform == 'win32':
        return f'{FILE_NAME}-windows-{arch}-setup.exe'
    return f'{FILE_NAME}-linux-{arch}.AppImage'


def _fetch_appimagetool() -> Path:
    """Download the pinned appimagetool for this machine, verified by sha256."""
    arch = 'aarch64' if platform.machine().lower() in ('aarch64', 'arm64') else 'x86_64'
    tool = DIST_DIR / 'tools' / f'appimagetool-{APPIMAGETOOL_VERSION}-{arch}.AppImage'
    expected = APPIMAGETOOL_SHA256[arch]
    if not tool.is_file() or hashlib.sha256(tool.read_bytes()).hexdigest() != expected:
        url = (
            'https://github.com/AppImage/appimagetool/releases/download/'
            f'{APPIMAGETOOL_VERSION}/appimagetool-{arch}.AppImage'
        )
        print(f'  Downloading {url}')
        tool.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise SystemExit(f'appimagetool sha256 is {actual}, expected {expected}')
        tool.write_bytes(data)
    tool.chmod(0o755)
    return tool


# Single prewarm script: load every model the app reaches for, so the
# downloads happen here (with retry-on-failure) rather than on first user
# click. BIRDNET_APP_DATA points at the build cache, so files land in a known
# location ready for PyInstaller bundling. The precision must match
# birdnet_lib._PRECISION or the bundle holds weights the app never asks for.
MODEL_PREWARM = textwrap.dedent("""
    import sys
    import birdnet
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
    # own version's list.
    for kind, version, backend, languages in (
        ('acoustic', '3.0', 'onnx', birdnet.VALID_MODEL_LANGUAGES_V3_0),
        ('geo', '3.0', 'onnx', birdnet.VALID_MODEL_LANGUAGES_V3_0),
        ('acoustic', '2.4', 'onnx', birdnet.VALID_MODEL_LANGUAGES_V2_4),
        ('geo', '2.4', 'onnx', birdnet.VALID_MODEL_LANGUAGES_V2_4),
    ):
        lang_dir = get_lang_dir(kind, version, backend)
        missing = sorted(
            lang for lang in languages
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
    disk to cache them and has no isolated venv to point at.
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
# language set. Glob patterns: the lib names its checksum-verified v3.0
# downloads model-fp32-<sha256 prefix>.onnx.
REQUIRED_MODEL_FILES: tuple[str, ...] = (
    'acoustic-models/v3.0/onnx/model-fp32*.onnx',
    'geo-models/v3.0/onnx/model-fp32*.onnx',
    'acoustic-models/v2.4/onnx/model-fp32.onnx',
    'geo-models/v2.4/onnx/model-fp32.onnx',
    # Perch has no geo model of its own and reuses v3.0's for the allow-list.
    'acoustic-models/perch-v2/onnx/model-fp32.onnx',
)


def _resolve_model_file(pattern: str) -> str:
    """The one cached file matching pattern, relative to BIRDNET_APP_DATA_CACHE.

    A model update leaves the previous release's file beside the new one, and
    Nuitka bundles the whole cache, so a second match is a stale copy that
    would ship.
    """
    found = sorted(BIRDNET_APP_DATA_CACHE.glob(pattern))
    if len(found) != 1:
        raise SystemExit(
            f'Expected exactly one model file matching {pattern} in '
            f'{BIRDNET_APP_DATA_CACHE}, found {[p.name for p in found]}. '
            'Delete the stale ones and rebuild.'
        )
    return found[0].relative_to(BIRDNET_APP_DATA_CACHE).as_posix()


def _create_dmg(app: Path, dmg: Path) -> None:
    """Pack app into dmg next to an Applications link, for drag-to-install.

    Plain hdiutil rather than create-dmg: no Finder AppleScript to hang on a
    headless runner, at the cost of the styled window. The app is moved into
    a staging folder and back, since copying it would cost 1.6 GB.

    GitHub's macOS runners intermittently fail hdiutil with "Resource busy"
    (actions/runner-images#7522), hence the retries.
    """
    stage = app.parent / 'dmg-stage'
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    staged_app = stage / app.name
    app.rename(staged_app)
    try:
        (stage / 'Applications').symlink_to('/Applications')
        cmd = [
            'hdiutil', 'create', '-volname', APP_NAME, '-srcfolder', str(stage),
            '-fs', 'HFS+', '-format', 'ULFO', '-ov', str(dmg),
        ]
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            try:
                run(cmd)
                break
            except subprocess.CalledProcessError:
                if attempt == max_attempts:
                    raise
                print(f'  hdiutil failed (attempt {attempt}/{max_attempts}), retrying...')
                time.sleep(10)
    finally:
        staged_app.rename(app)
        shutil.rmtree(stage)


def _verify_bundle(app_root: Path) -> None:
    """Fail the build if the bundled data did not make it into the output.

    A compiled app whose models are missing still starts and still runs: it
    just re-downloads model files on the user's first Analyze click, with the
    lib's progress bar going to a stderr that a GUI user won't see. So it is
    checked here.

    app_root is the tree the installer wraps: the macOS .app, or the .dist
    folder on Windows/Linux.
    """
    expected = {
        rel: (BIRDNET_APP_DATA_CACHE / rel).stat().st_size
        for rel in map(_resolve_model_file, REQUIRED_MODEL_FILES)
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
            'Used by the prewarm-models CI workflow to fill the cache that the test and '
            'build jobs restore, so they never trigger a bare, un-retried download.'
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
    run(['uv', 'venv', '--clear', VENV_DIR])

    print('  Syncing build venv (runtime deps + nuitka)')
    # The app is installed editable, so Nuitka finds its metadata and compiles
    # the version into importlib.metadata.version("pam-analyzer"). Uninstalled,
    # the app would report "unknown". Nuitka still compiles straight from src/,
    # which is also the only way the gitignored ui_*.py and *_rc.py modules
    # reach the build: a wheel would omit them, since hatchling selects files via git.
    # nuitka comes from the build group so uv.lock pins it.
    # --no-default-groups drops dev, which the build does not use: the compile
    # scripts call pyside6-uic and pyside6-rcc, both shipped with PySide6.
    run(
        [
            'uv', 'sync',
            '--python', str(python),
            '--no-default-groups',
            '--group', 'build',
        ],
        env=build_env,
    )
    app_version = _app_version(python)
    bundle_version, product_version = _numeric_versions(app_version)
    print(f'  Version  : {app_version}')

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

    print('  Running Nuitka')
    # Remove any output from previous builds
    for pattern in ('*.dist', '*.app', '*.build'):
        for stale in BUILD_DIR.glob(pattern):
            shutil.rmtree(stale)
    installer = DIST_DIR / _asset_name()
    installer.unlink(missing_ok=True)
    binary_name = f'{FILE_NAME}.exe' if is_win else FILE_NAME
    cmd = [
        'uv',
        'run',
        '--no-project',
        'python',
        '-m',
        'nuitka',
        # A folder on Windows/Linux, an app bundle on macOS
        '--mode=app-dist',
        '--remove-output',
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
        # Same for pandas and pyarrow, which birdnet imports only inside result
        # export methods this app never calls. Together they would add 99 MB.
        '--nofollow-import-to=pandas',
        '--nofollow-import-to=pyarrow',
        # birdnet resolves backends by name at runtime, so following its
        # imports statically does not reach every module it loads.
        '--include-package=birdnet',
        '--include-package-data=birdnet',
        # Toast icons are package data. Without them showing a toast stalls.
        '--include-package-data=pyqttoast',
        '--assume-yes-for-downloads',
        # Compiling the package directory rather than __main__.py keeps the
        # package context, so the relative imports inside the package resolve
        # as they do under `python -m pam_analyzer`.
        '--python-flag=-m',
        f'--output-dir={BUILD_DIR}',
        # Names the .app/.dist folder. Without it Nuitka uses the package name.
        f'--output-folder-name={FILE_NAME}',
        f'--output-filename={binary_name}',
    ]
    cmd += [f'--include-module={module}' for module in MODULES]
    # The model tree lands next to the binary, where app/__main__.py points
    # BIRDNET_APP_DATA at startup.
    cmd.append(f'--include-data-dir={MODEL_CACHE}=birdnet-models')
    cmd += [f'--include-data-files={src}={dest}' for src, dest in DATA]
    if is_mac:
        cmd += [
            f'--macos-app-icon={icon}',
            f'--macos-app-name={APP_NAME}',
            f'--macos-app-version={bundle_version}',
            f'--macos-signed-app-name={BUNDLE_ID}',
            # Not --macos-create-installer: Nuitka 4.3.4 hands create-dmg the
            # empty .dist folder instead of the .app. _create_dmg builds it.
        ]
    else:
        cmd += [
            f'--company-name={COMPANY_NAME}',
            f'--product-name={APP_NAME}',
            f'--product-version={product_version}',
        ]
        if is_win:
            cmd += [
                f'--windows-icon-from-ico={icon}',
                '--windows-console-mode=attach',  # Reuses parent console on terminal launch, no console on double-click
                '--msvc=latest',
                '--windows-create-installer',
                f'--windows-installer-output={installer}',
                # Per user, so neither installing nor updating needs admin rights
                '--windows-installer-mode=user',
                '--windows-installer-shortcuts=start-menu',
                # The uninstaller deletes the install folder recursively, and
                # the updater runs it. A user-picked folder like Documents
                # would go with it.
                '--windows-installer-no-user-change-install-dir',
            ]
        else:
            cmd += [
                f'--linux-icon={icon}',
                '--linux-create-installer',
                f'--linux-installer-output={installer}',
                f'--linux-installer-appimagetool-path={_fetch_appimagetool()}',
            ]
            # appimagetool is itself an AppImage, and CI runners may lack FUSE
            build_env['APPIMAGE_EXTRACT_AND_RUN'] = '1'
    cmd.append(ROOT_DIR / 'src' / 'pam_analyzer')
    run(cmd, env=build_env)

    app_tree = BUILD_DIR / f'{FILE_NAME}.app' if is_mac else BUILD_DIR / f'{FILE_NAME}.dist'
    if not app_tree.is_dir():
        raise SystemExit(f'Expected {app_tree}, not found')
    # On Windows/Linux this runs after Nuitka has already built the installer,
    # which is fine: a failure here stops the build before CI uploads anything.
    print('  Verifying bundled models')
    _verify_bundle(app_tree)
    if is_mac:
        print('  Creating DMG')
        _create_dmg(app_tree, installer)
    if not installer.is_file():
        raise SystemExit(f'Expected the installer at {installer}, not found')

    print(f'\nDone. Installer: {installer}\n      App:       {app_tree}')


if __name__ == '__main__':
    main()
