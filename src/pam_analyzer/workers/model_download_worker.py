import logging

from PySide6.QtCore import QObject, Signal, Slot

from ..domain import AnalysisRunner, CancelledError

_log = logging.getLogger(__name__)


class ModelDownloadWorker(QObject):
    """Downloads one engine's missing model files in the background, ahead of any run.

    Signals carry the engine key, so one controller can track several downloads.
    """

    progress = Signal(str, object, object)  # key, done bytes, total bytes
    finished = Signal(str, str)  # key, error message, empty unless the download failed

    def __init__(self, runner: AnalysisRunner) -> None:
        super().__init__()
        self._runner = runner

    @Slot()
    def run(self) -> None:
        error = ""
        try:
            self._runner.install_model(self._emit_progress, self.thread().isInterruptionRequested)
        except CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001  a dead worker thread would leave the link stuck on Cancel
            # str(): a record holding exc would keep its traceback, and this worker, alive
            _log.warning("%s: model download failed: %s", self._runner.model_key, str(exc))
            error = str(exc)
        self.finished.emit(self._runner.model_key, error)

    def _emit_progress(self, done: int, total: int) -> None:
        self.progress.emit(self._runner.model_key, done, total)
