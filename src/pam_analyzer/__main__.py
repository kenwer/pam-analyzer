"""Entry point for `python -m pam_analyzer` and PyInstaller bundles."""

import multiprocessing
import os
import sys

# On Windows GUI builds (pythonw.exe / a windowed PyInstaller bundle) the
# process has no console, so sys.stdout and sys.stderr are None. Any bare
# print() then raises "'NoneType' object has no attribute 'write'". The
# birdnet lib prints a run summary to stdout when show_stats="progress"
# (which we must pass to receive its progress_callback), and its workers
# print to stderr on error. Redirect both to the null device so those
# writes are silently discarded. os.devnull is a complete text stream, so
# isatty()/flush()/fileno() all behave. This runs before freeze_support()
# so spawned worker processes inherit the guard too. On a normal console
# run both streams are non-None and this is a no-op.
if sys.stdout is None or sys.stderr is None:
    _null = open(os.devnull, "w")  # noqa: SIM115  (lives for the process)
    if sys.stdout is None:
        sys.stdout = _null
    if sys.stderr is None:
        sys.stderr = _null

# On macOS and Windows, multiprocessing uses the 'spawn' start method. A
# frozen PyInstaller binary re-executes itself from scratch for every worker
# process, which would re-launch the full GUI. freeze_support() detects the
# worker-bootstrap argv sentinel and exits before any GUI code runs.
multiprocessing.freeze_support()

# PyInstaller freezes the app as a standalone executable, not a Python module.
# In that context, relative imports fail with "attempted relative import with
# no known parent package".
try:
    from pam_analyzer.app.__main__ import main  # absolute import works for frozen/script
except ImportError:
    from .app.__main__ import main  # fallback: relative import works for python -m package

# os._exit() bypasses Python's atexit chain, preventing the PySide6 atexit
# handler (SbkQtCoreModule___moduleShutdown) from running after Qt internals
# are already freed, which causes a SIGSEGV in QApplication::~QApplication().
# For a Qt GUI app this is safe because Qt already cleaned up through its own
# event system when app.exec() returned.
os._exit(main())
