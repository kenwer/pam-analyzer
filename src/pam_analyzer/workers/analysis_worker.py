import threading

from PySide6.QtCore import QObject, Signal, Slot

from ..domain import (
    AnalysisProgressSnapshot,
    AnalysisRunner,
    AnalysisSettings,
    Campaign,
    CampaignRunInput,
    CancelledError,
    FilterMode,
    Project,
)
from ..infrastructure import TomlCampaignRepository


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
    progress = Signal(object)   # AnalysisProgressSnapshot
    succeeded = Signal(object)  # AnalysisRunResult
    cancelled = Signal()
    failed = Signal(str)        # human-readable error message

    def __init__(
        self,
        runner: AnalysisRunner,
        campaign_repo: TomlCampaignRepository,
        project: Project,
        campaigns: list[Campaign],
        settings: AnalysisSettings,
    ) -> None:
        super().__init__()
        self._runner = runner
        self._campaigns_repo = campaign_repo
        self._project = project
        self._campaigns = campaigns
        self._settings = settings
        self._cancel_event = threading.Event()

    @Slot()
    def run(self) -> None:
        prog = _SignalProgress(self)
        try:
            inputs = [
                CampaignRunInput(
                    name=c.name,
                    folder=c.folder,
                    mode=c.species_filter_mode,
                    location=c.location,
                    species_list_text=(
                        self._campaigns_repo.read_species_list(c)
                        if c.species_filter_mode == FilterMode.LIST
                        else None
                    ),
                    must_have_species_text=(
                        self._campaigns_repo.read_must_have_species(c)
                        if c.species_filter_mode == FilterMode.LOCATION
                        else None
                    ),
                )
                for c in self._campaigns
            ]
            result = self._runner.run(
                campaigns=inputs,
                output_base=self._project.output_base,
                settings=self._settings,
                preferred_lang=self._project.preferred_species_lang,
                audio_root=self._project.audio_recordings_path,
                progress=prog,
            )
        except CancelledError:
            self.cancelled.emit()
            return
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(result)

    def request_cancel(self) -> None:
        self._cancel_event.set()
