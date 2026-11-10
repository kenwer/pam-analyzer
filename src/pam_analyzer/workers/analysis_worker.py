from PySide6.QtCore import QObject, Signal, Slot

from ..domain import (
    AnalysisProgressSnapshot,
    AnalysisRunner,
    AnalysisRunResult,
    AnalysisSettings,
    Campaign,
    CancelledError,
    Project,
    RunStatus,
)


class _SignalProgress:
    """AnalysisProgress port that forwards updates to worker signals.

    Lives in the worker thread; signal emissions are automatically queued
    across to the UI thread because the worker QObject was moved there.
    """

    def __init__(self, worker: AnalysisWorker) -> None:
        self._worker = worker

    def report(self, snapshot: AnalysisProgressSnapshot) -> None:
        self._worker.progress.emit(snapshot)

    def is_cancelled(self) -> bool:
        return self._worker.thread().isInterruptionRequested()


class AnalysisWorker(QObject):
    """Downloads the runner's missing model files, then runs the analysis."""

    progress = Signal(object)  # AnalysisProgressSnapshot
    downloadProgress = Signal(object, object)  # done, total bytes. object because a C++ int overflows past 2 GiB.
    finished = Signal(object)  # AnalysisRunResult carries both the completed campaigns and the status on how the run ended

    def __init__(
        self,
        runner: AnalysisRunner,
        project: Project,
        campaigns: list[Campaign],
        settings: AnalysisSettings,
    ) -> None:
        super().__init__()
        self._runner = runner
        self._project = project
        self._campaigns = campaigns
        self._settings = settings

    @Slot()
    def run(self) -> None:
        try:
            self._runner.install_model(self.downloadProgress.emit, self.thread().isInterruptionRequested)
            # The runner takes domain Campaign objects directly and loads each campaign's SpeciesFilter itself
            outcome = self._runner.run(
                campaigns=self._campaigns,
                settings=self._settings,
                preferred_lang=self._project.preferred_species_lang,
                progress=_SignalProgress(self),
            )
        except CancelledError:
            # A cancelled model download. The real runners return a CANCELLED
            # outcome rather than raising, a test fake may raise.
            outcome = AnalysisRunResult(status=RunStatus.CANCELLED)
        except Exception as exc:  # noqa: BLE001
            # Load-bearing: install_model's DownloadError, and setup before the
            # runner's own try/except (_load_model, file counting), raise straight out.
            # Turn that into a FAILED outcome instead of a dead worker thread
            # and a UI stuck on the progress page.
            outcome = AnalysisRunResult(status=RunStatus.FAILED, error=str(exc))
        self.finished.emit(outcome)
