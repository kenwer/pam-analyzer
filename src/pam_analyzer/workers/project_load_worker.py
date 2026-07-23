from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from ..infrastructure import (
    TomlCampaignRepository,
    TomlProjectRepository,
    load_project_bundle,
)


class ProjectLoadWorker(QObject):
    """Runs load_project_bundle() on a worker thread.

    Reading project.toml, discovering campaigns, and walking the audio tree
    are each a filesystem pass over the project folder, which can be slow on
    a network-mounted (e.g. CIFS) folder. This keeps that off the UI thread;
    the result is applied to AppState on the main thread by the caller.
    """

    succeeded = Signal(object)  # ProjectLoadResult
    failed = Signal(str)        # human-readable error message

    def __init__(
        self,
        project_repo: TomlProjectRepository,
        campaign_repo: TomlCampaignRepository,
        folder: Path,
    ) -> None:
        super().__init__()
        self._project_repo = project_repo
        self._campaign_repo = campaign_repo
        self._folder = folder

    @Slot()
    def run(self) -> None:
        try:
            result = load_project_bundle(self._project_repo, self._campaign_repo, self._folder)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Failed to open {self._folder.name}: {exc}")
            return
        self.succeeded.emit(result)
