"""Entry point for `python -m pam_analyzer` and PyInstaller bundles."""

import os

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
