"""Background model downloads, at most one per engine."""

from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, QThread, Signal

from ..domain import AnalysisRunner
from ..workers import ModelDownloadWorker


@dataclass
class _Download:
    name: str  # the engine's display name, for the quit prompt
    thread: QThread
    worker: ModelDownloadWorker
    done: int = 0
    total: int = 0  # 0 while queued behind another engine's install


class ModelDownloads(QObject):
    """Runs the downloads the user starts ahead of a run.

    They outlive a model or project switch, so several engines can download
    while the user looks at another one. Parent this under the main window so
    its running_threads() reaches the worker threads.
    """

    changed = Signal(str)  # engine key, on progress and when its download ends
    failed = Signal(str, str)  # engine key, error message

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._downloads: dict[str, _Download] = {}

    def start(self, runner: AnalysisRunner) -> None:
        key = runner.model_key
        if key in self._downloads:
            return
        thread = QThread(self)
        worker = ModelDownloadWorker(runner)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_finished)
        # Direct, so quit() runs on the worker thread and _on_finished's wait() cannot deadlock
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        self._downloads[key] = _Download(runner.model_info.name, thread, worker)
        self.changed.emit(key)
        thread.start()

    def cancel(self, key: str) -> None:
        if (download := self._downloads.get(key)) is not None:
            download.thread.requestInterruption()

    def progress(self, key: str) -> tuple[int, int] | None:
        """Done and total bytes of the engine's download, None when it is not downloading."""
        download = self._downloads.get(key)
        return None if download is None else (download.done, download.total)

    def labels(self) -> list[str]:
        """The running downloads, for the quit prompt. They belong to no project."""
        return [f"{d.name} model download" for d in self._downloads.values()]

    def _on_progress(self, key: str, done: int, total: int) -> None:
        if (download := self._downloads.get(key)) is not None:
            download.done, download.total = done, total
            self.changed.emit(key)

    def _on_finished(self, key: str, error: str) -> None:
        download = self._downloads.pop(key)
        download.thread.wait()  # quit() already ran on the worker thread
        download.thread.deleteLater()
        download.worker.deleteLater()
        if error:
            self.failed.emit(key, error)
        self.changed.emit(key)
