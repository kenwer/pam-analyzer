"""Shared, observable application state.

A single instance is created in bootstrap and injected into every panel.
Replaces the module-level globals from the original NiceGUI app.
"""

import logging
import time
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

_log = logging.getLogger(__name__)


class AppState(QObject):
    projectChanged = Signal(object)  # Project | None
    campaignsChanged = Signal(list)  # list[Campaign]
    currentCampaignChanged = Signal(object)  # Campaign | None
    statusMessage = Signal(str)
    errorOccurred = Signal(str)
    analysisStarted = Signal()
    analysisProgress = Signal(object)  # AnalysisProgressSnapshot
    analysisFinished = Signal(object)  # AnalysisRunResult | None
    lastAnalysisResultChanged = Signal(object)  # AnalysisRunResult | None
    importStarted = Signal(str, object)  # campaign name, ImportSource (which flavor of import)
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
    def last_analysis_result(self) -> AnalysisRunResult | None:
        return self._last_analysis_result

    @property
    def import_results(self) -> list[CardImportResult]:
        return list(self._import_results)

    @property
    def audio_inventory(self) -> AudioInventory:
        return self._audio_inventory

    def load_project(self, folder: Path) -> None:
        dbg = _log.isEnabledFor(logging.DEBUG)
        t0 = time.perf_counter() if dbg else 0.0
        try:
            project = self._project_repo.load(folder)
        except Exception as exc:
            self.errorOccurred.emit(f"Failed to open {folder.name}: {exc}")
            return
        if dbg:
            _log.debug("load_project: repo.load %.2fs", time.perf_counter() - t0)

        self._apply_project(project)

        t = time.perf_counter() if dbg else 0.0
        self.refresh_campaigns()
        if dbg:
            _log.debug("load_project: refresh_campaigns %.2fs", time.perf_counter() - t)

        t = time.perf_counter() if dbg else 0.0
        self.refresh_audio_inventory()
        if dbg:
            _log.debug("load_project: refresh_audio_inventory %.2fs", time.perf_counter() - t)

        t = time.perf_counter() if dbg else 0.0
        discovered = discover_analysis_result(project.folder)
        if dbg:
            _log.debug("load_project: discover_analysis_result %.2fs", time.perf_counter() - t)
            _log.debug("load_project: total %.2fs", time.perf_counter() - t0)

        self.set_last_analysis_result(discovered)
        self.statusMessage.emit(f"Opened {project.name}")

    def create_project(self, folder: Path) -> None:
        try:
            project = self._project_repo.create(folder)
        except Exception as exc:
            self.errorOccurred.emit(f"Failed to create {folder.name}: {exc}")
            return
        self._apply_project(project)
        self.refresh_campaigns()
        self.statusMessage.emit(f"Created {project.name}")

    def update_project(self, project: Project) -> None:
        """Replace the in-memory project after a user edit and persist it immediately.

        Deliberately does not go through _apply_project: that clears
        session-derived state (analysis results, inventory), which is only
        correct when switching projects. A settings edit cannot change the
        project folder, so nothing derived needs to be rebuilt.
        """
        if project == self._project:
            return
        self._project = project
        try:
            self._project_repo.save(project)
        except Exception as exc:
            self.errorOccurred.emit(f"Save failed: {exc}")
        self.projectChanged.emit(project)

    def close_project(self) -> None:
        """Drop the in-memory project. Settings are already persisted on every edit."""
        if self._project is None:
            return
        self._apply_project(None)
        self.refresh_campaigns()

    def update_birdnet_settings(
        self,
        *,
        analysis_model: str | None = None,
        min_conf: float | None = None,
        overlap: float | None = None,
        locales: tuple[str, ...] | None = None,
    ) -> None:
        """Persist BirdNET settings immediately without a full projectChanged broadcast."""
        if self._project is None:
            return
        fields: dict = {}
        if analysis_model is not None:
            fields["analysis_model"] = str(analysis_model)
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
        """Persist playback-padding values immediately.

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

    def refresh_analysis_result_from_disk(self) -> None:
        """Rebuild the analysis result from the on-disk CSV inventory.

        The panel calls this after every successful run so sibling-model
        CSVs the user accumulated in earlier runs stay visible alongside
        the new one. Discovery is the only source of truth for what's been
        produced; the in-memory result is just a view of it.
        """
        if self._project is None:
            self.set_last_analysis_result(None)
            return
        self.set_last_analysis_result(discover_analysis_result(self._project.folder))

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
        self._set_audio_inventory(discover_audio_inventory(self._project.folder))

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
            self._campaign_repo.discover(self._project.folder)
            if self._project is not None
            else []
        )
        self._apply_campaigns(campaigns)

    def _apply_project(self, project: Project | None) -> None:
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

    def _apply_campaigns(self, campaigns: list[Campaign]) -> None:
        self._campaigns = campaigns
        self.campaignsChanged.emit(list(campaigns))
        self.set_current_campaign(None)

    def _set_project_silent(self, project: Project) -> None:
        """Replace the in-memory project without broadcasting signals.
        Used for auto-save operations that must not trigger a full UI refresh."""
        self._project = project
