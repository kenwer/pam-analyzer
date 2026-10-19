from PySide6.QtCore import QThread


class ReadOnlyThread(QThread):
    """A worker thread that only reads, so quitting may abandon it mid-run.

    MainWindow waits for every other running thread before it closes, since
    killing one mid-write could leave a half-copied file or a truncated CSV.
    """
