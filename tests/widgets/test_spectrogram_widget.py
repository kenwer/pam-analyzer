"""SpectrogramWidget must stop its render thread when the widget goes away.

The thread is a parentless QThread owned by the Python wrapper. If the widget
is collected while the thread still runs, ~QThread calls abort() and the
process dies, so the thread has to be stopped as part of destruction rather
than only at application exit.
"""

import gc

from PySide6.QtWidgets import QVBoxLayout, QWidget

from pam_analyzer.widgets.spectrogram_widget import SpectrogramWidget


def test_dropping_the_widget_stops_its_render_thread(qtbot):
    widget = SpectrogramWidget()
    thread = widget._thread
    assert thread.isRunning()

    del widget
    gc.collect()

    assert not thread.isRunning()


def test_destroying_the_parent_stops_the_render_thread(qtbot):
    """The path a per-test MainWindow fixture takes.

    A child widget torn down with its parent never gets a close event, so
    closeEvent would not be enough here.
    """
    parent = QWidget()
    layout = QVBoxLayout(parent)
    widget = SpectrogramWidget()
    layout.addWidget(widget)
    thread = widget._thread
    assert thread.isRunning()

    del widget
    del layout
    del parent
    gc.collect()

    assert not thread.isRunning()
