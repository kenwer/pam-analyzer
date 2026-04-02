#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = []
# ///
"""Build PAM Analyzer distributable using PyInstaller.

Creates an isolated venv, installs the project and PyInstaller into it,
caches BirdNET model checkpoints, and runs PyInstaller.

Usage:
    uv run --script packaging/build.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

PACKAGING_DIR = Path(__file__).parent
ROOT_DIR = PACKAGING_DIR.parent

APP_NAME = 'pam-analyzer'
BIRDNET_CHECKPOINT_CACHE = PACKAGING_DIR / '.birdnet-checkpoints'

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


def run(cmd: list, env: dict | None = None) -> None:
    subprocess.run(cmd, check=True, env=env)


def main() -> None:
    venv_dir = PACKAGING_DIR / f'.venv-{APP_NAME}'
    is_mac = sys.platform == 'darwin'
    is_win = sys.platform == 'win32'
    python = venv_dir / ('Scripts/python.exe' if is_win else 'bin/python')
    venv_env = {**os.environ, 'VIRTUAL_ENV': str(venv_dir)}

    print(f'Building : {APP_NAME}')
    print(f'  Platform : {sys.platform}')

    print('  Creating venv')
    run(['uv', 'venv', '--python', '3.13', '--clear', venv_dir])

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
        shutil.copytree(venv_ckpt, BIRDNET_CHECKPOINT_CACHE)
        print(f'  Cached checkpoints to {BIRDNET_CHECKPOINT_CACHE}')

    print('  Generating app icon')
    src_png = PACKAGING_DIR / 'app.png'
    if is_mac:
        iconset_sizes = [16, 32, 128, 256, 512]
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / 'app.iconset'
            iconset.mkdir()
            for s in iconset_sizes:
                run(['sips', '-z', str(s), str(s), src_png, '--out', str(iconset / f'icon_{s}x{s}.png')])
                run(['sips', '-z', str(s * 2), str(s * 2), src_png, '--out', str(iconset / f'icon_{s}x{s}@2x.png')])
            icon = PACKAGING_DIR / 'app.icns'
            run(['iconutil', '-c', 'icns', '-o', icon, iconset])
    else:
        icon = PACKAGING_DIR / 'app.ico'
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

    splash_png = PACKAGING_DIR / 'splash.png'
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
        PACKAGING_DIR / 'dist',
        '--workpath',
        PACKAGING_DIR / 'build' / APP_NAME,
        '--specpath',
        PACKAGING_DIR / 'build' / APP_NAME,
        '--clean',
        '--noconfirm',
        '--name',
        APP_NAME,
        '--icon',
        icon,
        '--collect-all',
        'nicegui',
        '--collect-data',
        'birdnet_analyzer',
        '--add-data',
        f'{ROOT_DIR / "src" / "pam_analyzer" / "static"}:pam_analyzer/static',
        '--add-data',
        f'{ROOT_DIR / "CHANGELOG.md"}:.',
    ]
    if is_mac:
        cmd += ['--windowed']  # creates .app bundle, no Terminal window
    else:
        cmd += ['--onefile']  # single .exe on Windows / binary on Linux
    if is_win:
        cmd += [
            '--splash',
            splash_png,
        ]  # not supported on macOS (Tcl/Tk forbids secondary GUI threads) or Linux (python-build-standalone lacks _tkinter)
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
    cmd.append(ROOT_DIR / 'src' / 'pam_analyzer' / 'main.py')
    run(cmd, env=venv_env)

    print(f'\nDone. Binary is in {PACKAGING_DIR / "dist"}/')


if __name__ == '__main__':
    main()
