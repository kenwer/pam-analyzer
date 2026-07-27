"""Shared, observable application state.

A single instance is created in bootstrap and injected into every panel.
Replaces the module-level globals from the original NiceGUI app.
"""

import logging
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ..domain import (
    AnalysisRunResult,
    AudioInventory,
    Campaign,
    CardImportResult,
    FilterMode,
    LatLon,
    Project,
)
from ..infrastructure import (
    discover_analysis_result,
    discover_audio_inventory,
    load_project_bundle,
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

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
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
        """Load a project folder synchronously on the calling thread.

        For the UI, prefer routing folder opens through a ProjectLoadWorker
        and apply_loaded_project() instead: this blocks the caller for as
        long as the filesystem takes, which is a problem on slow or
        network-mounted (e.g. CIFS) folders.
        """
        try:
            result = load_project_bundle(folder)
        except Exception as exc:
            self.errorOccurred.emit(f"Failed to open {folder.name}: {exc}")
            return
        self.apply_loaded_project(
            result.project, result.campaigns, result.audio_inventory, result.analysis_result
        )

    def apply_loaded_project(
        self,
        project: Project,
        campaigns: list[Campaign],
        audio_inventory: AudioInventory,
        analysis_result: AnalysisRunResult | None,
    ) -> None:
        """Apply an already-loaded project bundle.

        Split out of load_project so a ProjectLoadWorker can do the
        filesystem work on a background thread and hand the results here to
        be applied on the UI thread.
        """
        self._apply_project(project)
        self._apply_campaigns(campaigns)
        self._set_audio_inventory(audio_inventory)
        self.set_last_analysis_result(analysis_result)
        self.statusMessage.emit(f"Opened {project.name}")

    def create_project(self, folder: Path) -> None:
        try:
            project = Project.create(folder)
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
            project.save()
        except Exception as exc:
            self.errorOccurred.emit(f"Save failed: {exc}")
        self.projectChanged.emit(project)

    def close_project(self) -> None:
        """Drop the in-memory project. Settings are already persisted on every edit."""
        if self._project is None:
            return
        self._apply_project(None)
        self.refresh_campaigns()

    def save_project_fields(self, **fields: object) -> None:
        """Persist partial Project edits immediately, without a projectChanged broadcast.

        Used by settings editors (analysis model, min confidence, overlap,
        locales, playback padding) that update their own widgets, so no other
        panel needs to react. Broadcasting on every slider tick would make
        every panel re-render mid-drag.
        """
        if self._project is None:
            return
        updated = replace(self._project, **fields)
        if updated == self._project:
            return
        self._set_project_silent(updated)
        try:
            updated.save()
        except Exception as exc:
            self.errorOccurred.emit(f"Failed to save settings: {exc}")

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
        inventory = discover_audio_inventory(self._project.folder)
        _log.debug(
            "refresh_audio_inventory: folder=%s -> %s",
            self._project.folder,
            {c.name: c.file_count for c in inventory.campaigns},
        )
        self._set_audio_inventory(inventory)

    def create_campaign(
        self, campaign: Campaign, species_text: str, must_have_text: str
    ) -> None:
        """Create a campaign folder, write its species file, and rebuild derived state.

        Raises FileExistsError if the folder already exists (nothing is created
        or refreshed in that case). If the folder is created but writing the
        species file fails, derived state is still rebuilt and the OSError is
        re-raised so the caller can warn the user while treating the campaign as
        created.
        """
        campaign.create()
        try:
            campaign.write_species_filter(species_text, must_have_text)
        finally:
            self._reload_campaign_derived_state()

    def update_campaign(
        self,
        existing: Campaign,
        new_name: str,
        mode: FilterMode,
        location: LatLon | None,
        species_text: str,
        must_have_text: str,
    ) -> None:
        """Apply an edit to an existing campaign, then rebuild derived state.

        Performs an optional rename, saves the settings change, and rewrites the
        species file. A failed rename (OSError) propagates before anything else
        changes, so nothing is refreshed in that case.
        """
        campaign = existing
        if new_name != existing.name:
            campaign = existing.rename(new_name)
        updated = replace(campaign, species_filter_mode=mode, location=location)
        updated.save()
        updated.write_species_filter(species_text, must_have_text)
        self._reload_campaign_derived_state()

    def rename_campaign(self, campaign: Campaign, new_name: str) -> None:
        """Rename a campaign folder, then rebuild derived state.

        Routed through here, rather than the repo directly, so the audio
        inventory is rebuilt alongside the campaign list. A rename changes the
        on-disk folder name, and the inventory is keyed by name, so skipping the
        inventory rebuild would strand the renamed campaign's cached file count
        under its old name (the same stale-count bug as a delete or create).
        Propagates OSError on a failed rename before anything is refreshed.
        """
        campaign.rename(new_name)
        self._reload_campaign_derived_state()

    def delete_campaign(self, campaign: Campaign) -> None:
        """Delete a campaign folder and rebuild derived state."""
        campaign.delete()
        self._reload_campaign_derived_state()

    def _reload_campaign_derived_state(self) -> None:
        """Rebuild everything derived from the set of campaign folders after a
        create, rename, or delete: the campaign list and the audio inventory.

        Centralized so a mutation cannot rebuild one and forget the other. That
        omission was the stale-file-count bug: a deleted campaign's cached
        inventory entry outlived the campaign and resurfaced under a reused name.
        """
        self.refresh_campaigns()
        self.refresh_audio_inventory()

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
            Campaign.discover(self._project.folder)
            if self._project is not None
            else []
        )
        _log.debug("refresh_campaigns: %s", [c.name for c in campaigns])
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
