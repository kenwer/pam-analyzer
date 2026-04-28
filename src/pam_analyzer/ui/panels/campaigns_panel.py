"""Campaigns panel: master/detail list for creating and editing campaigns."""

import dataclasses

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QInputDialog,
    QMenu,
    QMessageBox,
    QWidget,
)

from ...domain import Campaign, FilterMode, LatLon
from ...infrastructure import TomlCampaignRepository
from ..app_state import AppState
from .campaign_detail_widget import CampaignDetailWidget
from .ui_campaigns_panel import Ui_CampaignsPanel


class CampaignsPanel(QWidget):
    def __init__(
        self,
        app_state: AppState,
        campaign_repo: TomlCampaignRepository,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ui = Ui_CampaignsPanel()
        self.ui.setupUi(self)
        self.ui.splitter.setSizes([220, 780])

        self._app_state = app_state
        self._service = campaign_repo
        self._model = QStandardItemModel(self)

        self._detail = CampaignDetailWidget(self.ui.detail_container)
        self.ui.detail_layout.addWidget(self._detail)

        self.ui.campaign_list.setModel(self._model)
        self.ui.campaign_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self._wire_signals()
        self._setup_shortcuts()

    def _wire_signals(self) -> None:
        self._app_state.projectChanged.connect(self._on_project_changed)
        self._app_state.campaignsChanged.connect(self._rebuild_list)

        self.ui.new_button.clicked.connect(self._on_new)
        self.ui.campaign_list.selectionModel().currentChanged.connect(self._on_selection_changed)
        self.ui.campaign_list.customContextMenuRequested.connect(self._on_context_menu)
        self.ui.campaign_list.clicked.connect(self._on_list_clicked)

        self._detail.createRequested.connect(self._on_create_requested)
        self._detail.updateRequested.connect(self._on_update_requested)
        self._detail.deleteRequested.connect(self._on_delete_requested)
        self._detail.cancelled.connect(self._on_detail_cancelled)

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self).activated.connect(self._on_delete_shortcut)
        QShortcut(QKeySequence(Qt.Key.Key_F2), self).activated.connect(self._on_rename_shortcut)
        QShortcut(QKeySequence("Ctrl+N"), self).activated.connect(self._on_new)

    # AppState reactions

    def _on_project_changed(self, project: object) -> None:
        if project is None:
            self._model.removeRows(0, self._model.rowCount())
            self._detail.show_empty()

    def _rebuild_list(self, campaigns: list[Campaign]) -> None:
        sel = self.ui.campaign_list.selectionModel()
        sel.blockSignals(True)
        self._model.removeRows(0, self._model.rowCount())
        for c in campaigns:
            item = QStandardItem(c.name)
            item.setData(c, Qt.ItemDataRole.UserRole)
            if c.location:
                tip = f"📍 {c.location.latitude:.4f}, {c.location.longitude:.4f}"
            else:
                tip = "📋 Species list"
            item.setData(tip, Qt.ItemDataRole.ToolTipRole)
            self._model.appendRow(item)
        sel.blockSignals(False)

    # new / selection

    def _on_new(self) -> None:
        self.ui.campaign_list.clearSelection()
        self._detail.open_new(self._existing_names())

    def _on_selection_changed(self, current, _) -> None:
        campaign = self._campaign_at(current)
        if campaign is None:
            self._detail.show_empty()
        else:
            self._open_edit(campaign)

    def _open_edit(self, campaign: Campaign) -> None:
        species_text = (
            self._service.read_species_list(campaign)
            if campaign.species_filter_mode == FilterMode.LIST
            else ""
        )
        self._detail.open_edit(campaign, self._existing_names(), species_text)

    def _on_create_requested(
        self,
        name: str,
        mode: FilterMode,
        location: LatLon | None,
        species_text: str,
    ) -> None:
        project = self._app_state.project
        if project is None:
            return
        campaign = Campaign(
            name=name,
            folder=project.audio_recordings_path / name,
            species_filter_mode=mode,
            location=location,
        )
        try:
            self._service.create(campaign)
        except FileExistsError:
            QMessageBox.warning(
                self,
                "Create campaign",
                f'A folder named "{name}" already exists.',
            )
            return
        if mode == FilterMode.LIST:
            self._service.write_species_list(campaign, species_text)
        self._app_state.refresh_campaigns()
        self._select_by_name(name)

    def _on_update_requested(
        self,
        existing: Campaign,
        new_name: str,
        mode: FilterMode,
        location: LatLon | None,
        species_text: str,
    ) -> None:
        campaign = existing
        if new_name != existing.name:
            try:
                campaign = self._service.rename(existing, new_name)
            except OSError as exc:
                QMessageBox.warning(self, "Rename failed", str(exc))
                return
        updated = dataclasses.replace(campaign, species_filter_mode=mode, location=location)
        self._service.save(updated)
        if mode == FilterMode.LIST:
            self._service.write_species_list(updated, species_text)
        self._app_state.refresh_campaigns()
        self._select_by_name(new_name)

    def _on_delete_requested(self, campaign: Campaign) -> None:
        self._service.delete(campaign)
        self._app_state.refresh_campaigns()
        self._detail.show_empty()

    def _on_detail_cancelled(self) -> None:
        # Cancel closes the detail view and deselects the list item,
        # returning to the empty page. Re-clicking the same item re-opens
        # the edit page via _on_list_clicked.
        self.ui.campaign_list.clearSelection()
        self._detail.show_empty()

    def _on_list_clicked(self, index) -> None:
        # Re-open the edit page when the user clicks on an already-selected
        # item (currentChanged doesn't fire in that case).
        campaign = self._campaign_at(index)
        if campaign is not None:
            self._open_edit(campaign)

    # context menu & keyboard shortcuts

    def _on_context_menu(self, pos) -> None:
        index = self.ui.campaign_list.indexAt(pos)
        campaign = self._campaign_at(index)
        if campaign is None:
            return
        menu = QMenu(self)
        menu.addAction("Edit").triggered.connect(lambda: self._open_edit(campaign))
        menu.addAction("Rename…").triggered.connect(lambda: self._rename_campaign(campaign))
        menu.addAction("Delete…").triggered.connect(lambda: self._show_delete_confirm(campaign))
        menu.exec(self.ui.campaign_list.viewport().mapToGlobal(pos))

    def _on_delete_shortcut(self) -> None:
        campaign = self._selected_campaign()
        if campaign:
            self._show_delete_confirm(campaign)

    def _on_rename_shortcut(self) -> None:
        campaign = self._selected_campaign()
        if campaign:
            self._rename_campaign(campaign)

    def _show_delete_confirm(self, campaign: Campaign) -> None:
        audio_count = self._service.count_audio_files(campaign)
        self._detail.show_delete_confirm(campaign, audio_count)

    def _rename_campaign(self, campaign: Campaign) -> None:
        new_name, ok = QInputDialog.getText(self, "Rename campaign", "New name:", text=campaign.name)
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == campaign.name:
            return
        if new_name in self._existing_names():
            QMessageBox.warning(self, "Rename", f'A campaign named "{new_name}" already exists.')
            return
        try:
            self._service.rename(campaign, new_name)
        except OSError as exc:
            QMessageBox.warning(self, "Rename failed", str(exc))
            return
        self._app_state.refresh_campaigns()
        self._select_by_name(new_name)

    # helpers

    def _existing_names(self) -> list[str]:
        return [c.name for c in self._app_state.campaigns]

    def _selected_campaign(self) -> Campaign | None:
        indexes = self.ui.campaign_list.selectedIndexes()
        return self._campaign_at(indexes[0]) if indexes else None

    def _campaign_at(self, index) -> Campaign | None:
        if not index.isValid():
            return None
        return self._model.data(index, Qt.ItemDataRole.UserRole)

    def _select_by_name(self, name: str) -> None:
        for row in range(self._model.rowCount()):
            item = self._model.item(row)
            if item and item.text() == name:
                self.ui.campaign_list.setCurrentIndex(item.index())
                return
