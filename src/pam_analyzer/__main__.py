"""Entry point for `python -m pam_analyzer` and compiled builds."""

import multiprocessing
import os
import sys

# On Windows GUI builds (pythonw.exe / a windowed compiled build) the
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

# Only helps where sys.frozen is set, which Nuitka does not do. Kept for a
# source run under pythonw.exe, where it is the documented guard.
multiprocessing.freeze_support()

# On macOS and Windows multiprocessing spawns workers, and a worker re-executes
# this module to rebuild the parent's state. Without this guard it would reach
# the launch below, start a second full GUI, and that copy would spawn workers
# of its own until the machine dies. multiprocessing renames the module to
# __parents_main__ when it re-executes it, so the guard is False there and True
# in the real launch, compiled or not.
if __name__ == "__main__":
    # The compiled build runs this as a standalone executable, not a Python
    # module. In that context, relative imports fail with "attempted relative
    # import with no known parent package".
    try:
        from pam_analyzer.app.__main__ import main  # absolute import works for compiled/script
    except ImportError:
        from .app.__main__ import main  # fallback: relative import works for python -m package

    # os._exit() bypasses Python's atexit chain, preventing the PySide6 atexit
    # handler (SbkQtCoreModule___moduleShutdown) from running after Qt internals
    # are already freed, which causes a SIGSEGV in QApplication::~QApplication().
    # For a Qt GUI app this is safe because Qt already cleaned up through its own
    # event system when app.exec() returned.
    os._exit(main())
