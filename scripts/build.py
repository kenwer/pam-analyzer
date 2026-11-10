#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = []
# ///
"""Build the PAM Analyzer installer using Nuitka.

Creates an isolated venv, installs the project and Nuitka into it, compiles
the app, then wraps the result in the platform's installer: an Inno Setup
setup.exe on Windows, a DMG on macOS, an AppImage on Linux. The installer
lands in dist/ under the release asset name the in-app updater looks for.

No model weights ship in the bundle. Each engine downloads its files on its
first run (infrastructure/model_store.py).

Usage:
    uv run --script scripts/build.py [--stage {all,app,installer}]

On Windows, CI signs PAM-Analyzer.exe between the app and installer stages.
This script knows nothing about signing.
"""

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
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
    # perch_onnx.labels() reads this the same way.
    (
        ROOT_DIR / 'src' / 'pam_analyzer' / 'infrastructure' / 'data' / 'perch_v2_labels.csv',
        'pam_analyzer/infrastructure/data/perch_v2_labels.csv',
    ),
)


def _app_version(python: Path) -> str:
    """The version installed into the build venv, from pyproject.toml, like 0.8.0."""
    result = subprocess.run(
        [str(python), '-c', 'from importlib.metadata import version; print(version("pam-analyzer"))'],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _numeric_version(app_version: str) -> str:
    """The four-part a.b.c.0 form that Windows version resources require.

    Nuitka and Inno both write it, so the exe and the installer report the
    same product version, which the SignPath artifact configurations check.
    """
    return f'{app_version}.0'


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
    return f'{FILE_NAME}-linux-{"aarch64" if arch == "arm64" else arch}.AppImage'


def _fetch_appimagetool() -> Path:
    """Download the pinned appimagetool for this machine, verified by sha256.

    Returns the folder that holds it. Despite its help text, Nuitka 4.3.5
    treats --linux-installer-appimagetool-path as a PATH entry and looks for
    a file named exactly appimagetool in it.
    """
    arch = 'aarch64' if platform.machine().lower() in ('aarch64', 'arm64') else 'x86_64'
    tool = DIST_DIR / 'tools' / f'appimagetool-{APPIMAGETOOL_VERSION}-{arch}' / 'appimagetool'
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
    return tool.parent


def run(cmd: list, env: dict | None = None) -> None:
    subprocess.run(cmd, check=True, env=env)


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


ISS_SCRIPT = ROOT_DIR / 'packaging' / 'windows' / 'pam-analyzer.iss'


def _find_iscc() -> Path:
    """Inno Setup's compiler, preinstalled on GitHub's Windows runners."""
    found = shutil.which('ISCC')
    if found:
        return Path(found)
    program_files = Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'))
    default = program_files / 'Inno Setup 6' / 'ISCC.exe'
    if default.is_file():
        return default
    raise SystemExit('Inno Setup not found. Install it with: winget install JRSoftware.InnoSetup')


def _create_windows_installer(dist: Path, installer: Path, icon: Path, app_version: str) -> None:
    """Compile packaging/windows/pam-analyzer.iss over the Nuitka .dist folder."""
    run([
        str(_find_iscc()),
        f'/DAppVersion={app_version}',
        f'/DNumericVersion={_numeric_version(app_version)}',
        f'/DSourceDir={dist}',
        f'/DOutputDir={installer.parent}',
        f'/DOutputBaseFilename={installer.stem}',
        f'/DIconFile={icon}',
        str(ISS_SCRIPT),
    ])


def _verify_bundle(app_root: Path) -> None:
    """Fail the build if the bundled data files did not make it into the output.

    app_root is the tree the installer wraps: the macOS .app, or the .dist
    folder on Windows/Linux.
    """
    for _src, dest in DATA:
        if not list(app_root.glob(f'**/{dest}')):
            raise SystemExit(
                f'{dest} is missing from the bundle. Check its --include-data-files entry.'
            )

    print(f'  Verified bundle: {len(DATA)} data files')


STAGES = ('all', 'app', 'installer')


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Build the PAM Analyzer installer.')
    parser.add_argument(
        '--stage',
        choices=STAGES,
        default='all',
        help=(
            'Windows only: "app" stops after Nuitka, "installer" packs an existing '
            '.dist folder with Inno Setup. Default: the whole build.'
        ),
    )
    args = parser.parse_args(argv)
    if args.stage != 'all' and sys.platform != 'win32':
        parser.error(f'--stage {args.stage} is only supported on Windows')
    return args


def _build_env() -> dict[str, str]:
    # Force uv to use our build venv instead of the default project .venv.
    # VIRTUAL_ENV is for general python tool awareness, UV_PROJECT_ENVIRONMENT
    # is specifically to stop uv from auto-discovering the root .venv.
    return {
        **os.environ,
        'VIRTUAL_ENV': str(VENV_DIR),
        'UV_PROJECT_ENVIRONMENT': str(VENV_DIR),
    }


def _venv_python() -> Path:
    return VENV_DIR / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')


def _app_tree() -> Path:
    """The tree the installer wraps: the macOS .app, or the .dist folder."""
    suffix = '.app' if sys.platform == 'darwin' else '.dist'
    return BUILD_DIR / f'{FILE_NAME}{suffix}'


def _prepare_venv(python: Path, build_env: dict[str, str]) -> str:
    """Create the build venv, compile the Qt files into src/, return the app version."""
    print('  Creating venv')
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    run(['uv', 'venv', '--clear', VENV_DIR])

    print('  Syncing build venv (runtime deps + nuitka)')
    # The app is installed editable, so Nuitka finds its metadata and compiles
    # the version into importlib.metadata.version("pam-analyzer"). Uninstalled,
    # the app would report "unknown". Nuitka still compiles straight from src/,
    # which is also the only way the gitignored ui_*.py and *_rc.py modules
    # reach the build, since they are generated after the sync.
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
    print(f'  Version  : {app_version}')

    print('  Compiling Qt resources and UI files')
    # .ui -> ui_*.py and .qrc -> *_rc.py must exist in src/ before Nuitka reads
    # that tree, since it compiles the sources rather than an installed copy.
    run(['uv', 'run', '--no-project', 'python', str(PACKAGING_DIR / 'compile_ui.py')], env=build_env)
    run(['uv', 'run', '--no-project', 'python', str(PACKAGING_DIR / 'compile_qrc.py')], env=build_env)
    return app_version


def _windows_icon() -> Path:
    return BUILD_DIR / 'app.ico'


def _generate_icon() -> Path:
    print('  Generating app icon')
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    src_png = APP_ICON_PNG
    if sys.platform == 'darwin':
        iconset_sizes = [16, 32, 128, 256, 512]
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / 'app.iconset'
            iconset.mkdir()
            for s in iconset_sizes:
                run(['sips', '-z', str(s), str(s), src_png, '--out', str(iconset / f'icon_{s}x{s}.png')])
                run(['sips', '-z', str(s * 2), str(s * 2), src_png, '--out', str(iconset / f'icon_{s}x{s}@2x.png')])
            icon = BUILD_DIR / 'app.icns'
            run(['iconutil', '-c', 'icns', '-o', icon, iconset])
        return icon
    if sys.platform == 'win32':
        icon = _windows_icon()
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
        return icon
    return src_png  # Nuitka takes a PNG directly on Linux


def _run_nuitka(icon: Path, app_version: str, installer: Path, build_env: dict[str, str]) -> None:
    is_mac = sys.platform == 'darwin'
    is_win = sys.platform == 'win32'
    print('  Running Nuitka')
    # Remove any output from previous builds
    for pattern in ('*.dist', '*.app', '*.build'):
        for stale in BUILD_DIR.glob(pattern):
            shutil.rmtree(stale)
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
    cmd += [f'--include-data-files={src}={dest}' for src, dest in DATA]
    if is_mac:
        cmd += [
            f'--macos-app-icon={icon}',
            f'--macos-app-name={APP_NAME}',
            f'--macos-app-version={app_version}',
            f'--macos-signed-app-name={BUNDLE_ID}',
            # Not --macos-create-installer: Nuitka 4.3.5 hands create-dmg the
            # empty .dist folder instead of the .app. _create_dmg builds it.
        ]
    else:
        cmd += [
            f'--company-name={COMPANY_NAME}',
            f'--product-name={APP_NAME}',
            f'--product-version={_numeric_version(app_version) if is_win else app_version}',
        ]
        if is_win:
            cmd += [
                f'--windows-icon-from-ico={icon}',
                '--windows-console-mode=attach',  # Reuses parent console on terminal launch, no console on double-click
                '--msvc=latest',
                # Not --windows-create-installer: Nuitka's NSIS script cannot
                # clean the folder or wait for a running app. See _create_windows_installer.
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


def _finish_installer(app_tree: Path, installer: Path, icon: Path, app_version: str) -> None:
    """Verify the app tree, then wrap it in the platform installer."""
    # On Linux this runs after Nuitka has already built the AppImage, which is
    # fine: a failure here stops the build before CI uploads anything.
    print('  Verifying bundled data files')
    _verify_bundle(app_tree)
    if sys.platform == 'darwin':
        print('  Creating DMG')
        _create_dmg(app_tree, installer)
    elif sys.platform == 'win32':
        print('  Creating installer')
        _create_windows_installer(app_tree, installer, icon, app_version)
    if not installer.is_file():
        raise SystemExit(f'Expected the installer at {installer}, not found')


def main(argv: list[str] | None = None) -> None:
    stage = _parse_args(argv).stage
    python = _venv_python()
    build_env = _build_env()
    app_tree = _app_tree()
    installer = DIST_DIR / _asset_name()

    print(f'Building : {APP_NAME}')
    print(f'  Platform : {sys.platform}')
    print(f'  Stage    : {stage}')

    if stage == 'installer':
        # Packs the .dist folder an earlier --stage app left, which CI may
        # have changed since by putting the signed exe into it.
        if not app_tree.is_dir():
            raise SystemExit(f'Expected {app_tree}, not found. Run --stage app first.')
        app_version = _app_version(python)
        print(f'  Version  : {app_version}')
        installer.unlink(missing_ok=True)
        _finish_installer(app_tree, installer, _windows_icon(), app_version)
        print(f'\nDone. Installer: {installer}')
        return

    app_version = _prepare_venv(python, build_env)
    icon = _generate_icon()
    installer.unlink(missing_ok=True)
    _run_nuitka(icon, app_version, installer, build_env)
    if not app_tree.is_dir():
        raise SystemExit(f'Expected {app_tree}, not found')

    if stage == 'app':
        print('  Verifying bundled data files')
        _verify_bundle(app_tree)
        print(f'\nDone. App: {app_tree}')
        return

    _finish_installer(app_tree, installer, icon, app_version)
    print(f'\nDone. Installer: {installer}\n      App:       {app_tree}')


if __name__ == '__main__':
    main()
