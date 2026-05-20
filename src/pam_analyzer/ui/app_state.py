"""Shared, observable application state.

A single instance is created in bootstrap and injected into every panel.
Replaces the module-level globals from the original NiceGUI app.
"""

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ..domain import (
    AnalysisRunResult,
    AudioInventory,
    Campaign,
    CardImportResult,
    Project,
)
from ..infrastructure import (
    TomlCampaignRepository,
    TomlProjectRepository,
    discover_analysis_result,
    discover_audio_inventory,
)


class AppState(QObject):
    projectChanged = Signal(object)  # Project | None
    projectDirtyChanged = Signal(bool)
    campaignsChanged = Signal(list)  # list[Campaign]
    currentCampaignChanged = Signal(object)  # Campaign | None
    statusMessage = Signal(str)
    errorOccurred = Signal(str)
    analysisStarted = Signal()
    analysisProgress = Signal(object)  # AnalysisProgressSnapshot
    analysisFinished = Signal(object)  # AnalysisRunResult | None
    lastAnalysisResultChanged = Signal(object)  # AnalysisRunResult | None
    importStarted = Signal(str)  # campaign name being watched
    importFinished = Signal()
    importResultsChanged = Signal(list)  # list[CardImportResult]
    audioInventoryChanged = Signal(object)  # AudioInventory

    def __init__(
        self,
        project_repo: TomlProjectRepository,
        campaign_repo: TomlCampaignRepository,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._project_repo = project_repo
        self._campaign_repo = campaign_repo
        self._project: Project | None = None
        self._dirty: bool = False
        self._campaigns: list[Campaign] = []
        self._current_campaign: Campaign | None = None
        self._last_analysis_result: AnalysisRunResult | None = None
        self._import_results: list[CardImportResult] = []
        self._audio_inventory: AudioInventory = AudioInventory()

    @property
    def project(self) -> Project | None:
        return self._project

    @property
    def campaigns(self) -> list[Campaign]:
        return list(self._campaigns)

    @property
    def current_campaign(self) -> Campaign | None:
        return self._current_campaign

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    @property
    def last_analysis_result(self) -> AnalysisRunResult | None:
        return self._last_analysis_result

    @property
    def import_results(self) -> list[CardImportResult]:
        return list(self._import_results)

    @property
    def audio_inventory(self) -> AudioInventory:
        return self._audio_inventory

    def load_project(self, path: Path) -> None:
        try:
            project = self._project_repo.load(path)
        except Exception as exc:
            self.errorOccurred.emit(f"Failed to open {path.name}: {exc}")
            return
        self._apply_project(project, dirty=False)
        self.refresh_campaigns()
        self.refresh_audio_inventory()
        discovered = discover_analysis_result(project.output_base, project.name)
        if discovered is not None:
            self.set_last_analysis_result(discovered)
        self.statusMessage.emit(f"Opened {path.name}")

    def create_project(self, path: Path) -> None:
        try:
            project = self._project_repo.create(path)
        except Exception as exc:
            self.errorOccurred.emit(f"Failed to create {path.name}: {exc}")
            return
        self._apply_project(project, dirty=False)
        self.refresh_campaigns()
        self.statusMessage.emit(f"Created {path.name}")

    def update_project(self, project: Project) -> None:
        """Replace the in-memory project after a user edit. Marks dirty, does not persist.

        Re-discovers campaigns when the audio root or output base changes.
        Callers must invoke save_project() to flush to disk.
        """
        previous = self._project
        if project == previous:
            return
        self._apply_project(project, dirty=True)
        if (
            previous is None
            or previous.audio_recordings_path != project.audio_recordings_path
            or previous.output_base != project.output_base
        ):
            self.refresh_campaigns()

    def save_project(self) -> None:
        if self._project is None:
            return
        try:
            self._project_repo.save(self._project)
        except Exception as exc:
            self.errorOccurred.emit(f"Save failed: {exc}")
            return
        self._set_dirty(False)
        self.statusMessage.emit(f"Saved {self._project.path.name}")

    def save_project_as(self, new_path: Path) -> None:
        if self._project is None:
            return
        try:
            project = self._project_repo.save_as(self._project, new_path)
        except Exception as exc:
            self.errorOccurred.emit(f"Save failed: {exc}")
            return
        self._apply_project(project, dirty=False)
        self.statusMessage.emit(f"Saved as {new_path.name}")

    def close_project(self) -> None:
        """Drop the in-memory project. Does not prompt about unsaved edits.
        The caller is responsible for confirming with the user."""
        if self._project is None:
            return
        self._apply_project(None, dirty=False)
        self.refresh_campaigns()

    def update_birdnet_settings(
        self,
        *,
        min_conf: float | None = None,
        overlap: float | None = None,
        locales: tuple[str, ...] | None = None,
    ) -> None:
        """Persist BirdNET settings immediately without marking the project dirty."""
        if self._project is None:
            return
        fields: dict = {}
        if min_conf is not None:
            fields["birdnet_min_conf"] = float(min_conf)
        if overlap is not None:
            fields["birdnet_overlap"] = float(overlap)
        if locales is not None:
            fields["birdnet_locales"] = tuple(locales)
        if not fields:
            return
        updated = replace(self._project, **fields)
        if updated == self._project:
            return
        self._set_project_silent(updated)
        try:
            self._project_repo.save(updated)
        except Exception as exc:
            self.errorOccurred.emit(f"Failed to save BirdNET settings: {exc}")

    def update_padding(self, before: float, after: float) -> None:
        """Persist playback-padding values immediately without marking the project dirty.

        Skips the projectChanged broadcast because only the audio player cares
        about padding and the panel updates its own spinboxes directly.
        """
        if self._project is None:
            return
        updated = replace(
            self._project,
            snippet_padding_before=float(before),
            snippet_padding_after=float(after),
        )
        if updated == self._project:
            return
        self._set_project_silent(updated)
        try:
            self._project_repo.save(updated)
        except Exception as exc:
            self.errorOccurred.emit(f"Failed to save padding: {exc}")

    def set_last_analysis_result(self, result: AnalysisRunResult | None) -> None:
        if result is self._last_analysis_result:
            return
        self._last_analysis_result = result
        self.lastAnalysisResultChanged.emit(result)

    def append_import_result(self, result: CardImportResult) -> None:
        self._import_results.append(result)
        self.importResultsChanged.emit(list(self._import_results))
        # An import finished (success or error): files may have landed, so the
        # on-disk inventory probably changed. Re-scan rather than trying to
        # mutate the cached snapshot, since the diff is small and correctness
        # matters more than the rescan cost here.
        self.refresh_audio_inventory()

    def refresh_audio_inventory(self) -> None:
        if self._project is None:
            self._set_audio_inventory(AudioInventory())
            return
        self._set_audio_inventory(discover_audio_inventory(self._project.audio_recordings_path))

    def _set_audio_inventory(self, inventory: AudioInventory) -> None:
        self._audio_inventory = inventory
        self.audioInventoryChanged.emit(inventory)

    def set_current_campaign(self, campaign: Campaign | None) -> None:
        if campaign is self._current_campaign:
            return
        self._current_campaign = campaign
        self.currentCampaignChanged.emit(campaign)

    def refresh_campaigns(self) -> None:
        campaigns = (
            self._campaign_repo.discover(self._project.audio_recordings_path)
            if self._project is not None
            else []
        )
        self._apply_campaigns(campaigns)

    def _apply_project(self, project: Project | None, *, dirty: bool = False) -> None:
        self._project = project
        # Clear session-scoped derived state before emitting projectChanged so
        # any panel that re-reads these properties during its render sees the
        # new (empty) session, not the previous project's results.
        if self._last_analysis_result is not None:
            self._last_analysis_result = None
            self.lastAnalysisResultChanged.emit(None)
        if self._import_results:
            self._import_results = []
            self.importResultsChanged.emit([])
        if self._audio_inventory.campaigns:
            self._audio_inventory = AudioInventory()
            self.audioInventoryChanged.emit(self._audio_inventory)
        self.projectChanged.emit(project)
        self._set_dirty(dirty)

    def _apply_campaigns(self, campaigns: list[Campaign]) -> None:
        self._campaigns = campaigns
        self.campaignsChanged.emit(list(campaigns))
        self.set_current_campaign(None)

    def _set_project_silent(self, project: Project) -> None:
        """Replace the in-memory project without broadcasting signals.
        Used for auto-save operations that must not trigger a full UI refresh."""
        self._project = project

    def _set_dirty(self, value: bool) -> None:
        if value == self._dirty:
            return
        self._dirty = value
        self.projectDirtyChanged.emit(value)
