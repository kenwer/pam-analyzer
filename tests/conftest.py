"""Shared test fixtures.

`qtbot` is auto-provided by pytest-qt. We force the offscreen platform so
headless CI works the same as a developer laptop.
"""

import gc
import os

import pytest
from PySide6.QtCore import QThread

from pam_analyzer.infrastructure.birdnet_runner import MODEL_KEY

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# The model key a run writes today, derived from the shipped runner rather
# than copied. Tests that only need "a detections CSV" should use this.
CURRENT_MODEL_KEY = MODEL_KEY

# Keys the app can still read but no longer writes. Only tests whose subject
# is that continuity should name one of these, and they should say so.
RETIRED_MODEL_KEYS = ("BirdNET-2.4", "Perch-2.0")


@pytest.fixture(autouse=True)
def _stop_qt_threads():
    """Stop every QThread a test left running before the next test runs.

    Widgets such as SpectrogramWidget start a worker QThread that the running
    app stops via QApplication.aboutToQuit. Under pytest there is no event loop
    and that signal never fires, so each test's widgets keep their threads
    running. When such a widget is later garbage-collected (mid-run, or by
    PySide6's atexit handler at shutdown) its QThread C++ object is destroyed,
    and ~QThread on a still-running thread calls abort(). Quitting the threads
    after each test, while the interpreter is healthy, avoids that crash.
    """
    yield
    for obj in gc.get_objects():
        if not isinstance(obj, QThread):
            continue
        try:
            running = obj.isRunning()
        except RuntimeError:
            continue  # underlying C++ object already gone
        if running:
            obj.quit()
            obj.wait()
