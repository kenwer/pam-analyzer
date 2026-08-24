import threading

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

    def __init__(self, worker: "AnalysisWorker") -> None:
        self._worker = worker

    def report(self, snapshot: AnalysisProgressSnapshot) -> None:
        self._worker.progress.emit(snapshot)

    def is_cancelled(self) -> bool:
        return self._worker._cancel_event.is_set()


class AnalysisWorker(QObject):
    progress = Signal(object)  # AnalysisProgressSnapshot
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
        self._cancel_event = threading.Event()

    @Slot()
    def run(self) -> None:
        prog = _SignalProgress(self)
        try:
            # The runner takes domain Campaign objects directly and loads each campaign's SpeciesFilter itself
            outcome = self._runner.run(
                campaigns=self._campaigns,
                settings=self._settings,
                preferred_lang=self._project.preferred_species_lang,
                progress=prog,
            )
        except CancelledError:
            # Defensive: BirdnetRunner returns a CANCELLED outcome rather
            # than raising, so this only fires for a different runner (or the
            # test fake) that signals cancellation by raising.
            outcome = AnalysisRunResult(status=RunStatus.CANCELLED)
        except Exception as exc:  # noqa: BLE001
            # Load-bearing: setup before the runner's own try/except
            # (_load_model, file counting) can raise straight out of run().
            # Turn that into a FAILED outcome instead of a dead worker thread
            # and a UI stuck on the progress page.
            outcome = AnalysisRunResult(status=RunStatus.FAILED, error=str(exc))
        self.finished.emit(outcome)

    def request_cancel(self) -> None:
        self._cancel_event.set()
