"""Deleting a widget while another thread garbage-collects, checked in a subprocess.

A layout created without a parent keeps the widgets and layouts added to it as
Python-owned references. When Qt later deletes that layout, Shiboken releases
them one at a time and frees each with the GIL released, while the freed ones
are still listed on the layout's wrapper. A collection on another thread in
that window follows a dead pointer and segfaults, which cannot be observed
from inside the test process.
"""

import subprocess
import sys
import textwrap

_SCRIPT = """
    import faulthandler
    import gc
    import threading

    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    from pam_analyzer.widgets.audio_player import AudioPlayerPanel

    faulthandler.enable()
    app = QApplication(["teardown", "-platform", "offscreen"])
    stop = threading.Event()


    def collect():
        while not stop.is_set():
            # Zero-filled blocks in every small size class, so a wrapper the
            # main thread just freed is reused before the collection reads it.
            blocks = [bytearray(n - 1) for n in range(16, 257, 16)]
            gc.collect()


    # Everything imported so far is left out of the collections, which keeps
    # each one short enough to land inside the deletion.
    gc.freeze()
    # The wrappers stay referenced so that Qt, not Python, deletes the widgets.
    widgets = [AudioPlayerPanel() for _ in range(30)]
    collector = threading.Thread(target=collect)
    collector.start()
    for widget in widgets:
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    stop.set()
    collector.join()
    print("SURVIVED")
"""


def test_deferred_delete_survives_a_collection_on_another_thread() -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_SCRIPT)],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
    )
    assert "SURVIVED" in result.stdout, result.stderr[:2000]
