"""Shared, observable application state.

A single instance is created in `bootstrap.py` and injected into every panel.
Replaces the module-level globals from the original NiceGUI app.
"""

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ..domain import Campaign, Project
from ..infrastructure import TomlCampaignRepository, TomlProjectRepository


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

    def load_project(self, path: Path) -> None:
        try:
            project = self._project_repo.load(path)
        except Exception as exc:
            self.errorOccurred.emit(f"Failed to open {path.name}: {exc}")
            return
        self._project = project
        self._set_dirty(False)
        self.projectChanged.emit(project)
        self.refresh_campaigns()
        self.statusMessage.emit(f"Opened {path.name}")

    def create_project(self, path: Path) -> None:
        try:
            project = self._project_repo.create(path)
        except Exception as exc:
            self.errorOccurred.emit(f"Failed to create {path.name}: {exc}")
            return
        self._project = project
        self._set_dirty(False)
        self.projectChanged.emit(project)
        self.refresh_campaigns()
        self.statusMessage.emit(f"Created {path.name}")

    def update_project(self, project: Project) -> None:
        """Replace the in-memory project (e.g. after the user edits settings).

        Emits projectChanged and marks the project dirty. Does not persist.
        Callers must invoke save_project() (or File / Save) to flush to disk.
        Re-discovers campaigns when the audio root changes.
        """
        if project == self._project:
            return
        previous = self._project
        self._project = project
        self._set_dirty(True)
        self.projectChanged.emit(project)
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

    def update_birdnet_settings(
        self,
        *,
        min_conf: float | None = None,
        overlap: float | None = None,
        locales: tuple[str, ...] | None = None,
    ) -> None:
        """Auto-save BirdNET settings into the project (like update_padding)."""
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
        self._project = updated
        try:
            self._project_repo.save(updated)
        except Exception as exc:
            self.errorOccurred.emit(f"Failed to save BirdNET settings: {exc}")

    def update_padding(self, before: float, after: float) -> None:
        """Persist new playback-padding values to the in-memory project and
        write them to disk immediately. Skips the projectChanged broadcast,
        since only the audio player cares about padding and the panel updates
        it directly.
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
        self._project = updated
        try:
            self._project_repo.save(updated)
        except Exception as exc:
            self.errorOccurred.emit(f"Failed to save padding: {exc}")

    def close_project(self) -> None:
        """Drop the in-memory project. Does not prompt about unsaved edits.
        The caller is responsible for confirming with the user."""
        if self._project is None:
            return
        self._project = None
        self._set_dirty(False)
        self.projectChanged.emit(None)
        self.refresh_campaigns()

    def save_project_as(self, new_path: Path) -> None:
        if self._project is None:
            return
        try:
            project = self._project_repo.save_as(self._project, new_path)
        except Exception as exc:
            self.errorOccurred.emit(f"Save failed: {exc}")
            return
        self._project = project
        self._set_dirty(False)
        self.projectChanged.emit(project)
        self.statusMessage.emit(f"Saved as {new_path.name}")

    def _set_dirty(self, value: bool) -> None:
        if value == self._dirty:
            return
        self._dirty = value
        self.projectDirtyChanged.emit(value)

    def set_current_campaign(self, campaign: Campaign | None) -> None:
        if campaign is self._current_campaign:
            return
        self._current_campaign = campaign
        self.currentCampaignChanged.emit(campaign)

    def refresh_campaigns(self) -> None:
        if self._project is None:
            self._campaigns = []
        else:
            self._campaigns = self._campaign_repo.discover(self._project.audio_recordings_path)
        self.campaignsChanged.emit(self._campaigns)
        # Reset selection when project changes
        self.set_current_campaign(None)
