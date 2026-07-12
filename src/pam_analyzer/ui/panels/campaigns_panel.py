"""Campaigns panel: master/detail list for creating and editing campaigns."""

import dataclasses
from enum import Enum

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import (
    QActionGroup,
    QDesktopServices,
    QKeySequence,
    QShortcut,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QInputDialog,
    QMenu,
    QMessageBox,
    QWidget,
)

from ...domain import Campaign, FilterMode, LatLon
from ...infrastructure import TomlCampaignRepository
from ...workers import ImportOrchestrator
from ..app_state import AppState
from ..settings import AppSettings
from .campaign_detail_widget import CampaignDetailWidget
from .ui_campaigns_panel import Ui_CampaignsPanel


class CampaignSortOrder(Enum):
    DATE_MODIFIED_DESC = "date_modified_desc"
    DATE_MODIFIED_ASC = "date_modified_asc"
    NAME_ASC = "name_asc"
    NAME_DESC = "name_desc"


SORT_ORDER_LABELS = {
    CampaignSortOrder.DATE_MODIFIED_DESC: "Date Modified (Newest First)",
    CampaignSortOrder.DATE_MODIFIED_ASC: "Date Modified (Oldest First)",
    CampaignSortOrder.NAME_ASC: "Name (A to Z)",
    CampaignSortOrder.NAME_DESC: "Name (Z to A)",
}


class CampaignsPanel(QWidget):
    def __init__(
        self,
        app_state: AppState,
        campaign_repo: TomlCampaignRepository,
        orchestrator: ImportOrchestrator,
        settings: AppSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ui = Ui_CampaignsPanel()
        self.ui.setupUi(self)
        self.ui.splitter.setSizes([220, 780])

        self._app_state = app_state
        self._service = campaign_repo
        self._settings = settings
        self._model = QStandardItemModel(self)
        # Raw campaign list as last received from AppState, in repository
        # order (mtime descending). Re-sorted into the model on demand so
        # switching sort order doesn't require a fresh discover() call.
        self._campaigns: list[Campaign] = []
        try:
            self._sort_order = CampaignSortOrder(settings.campaign_sort_order)
        except ValueError:
            self._sort_order = CampaignSortOrder.NAME_ASC
        # Set true while we revert a selection programmatically so the
        # selectionChanged handler ignores the synthetic event.
        self._reverting_selection = False
        # Set true while the edit or new form is open; disables the list and
        # new button so the user cannot switch campaigns mid-edit.
        self._list_locked = False

        self._detail = CampaignDetailWidget(
            app_state,
            orchestrator,
            self.ui.detail_container,
        )
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

        self._detail.editingChanged.connect(self._set_list_locked)
        self._detail.createRequested.connect(self._on_create_requested)
        self._detail.updateRequested.connect(self._on_update_requested)
        self._detail.deleteRequested.connect(self._on_delete_requested)
        self._detail.deleteConfirmRequested.connect(self._show_delete_confirm)
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
        self._campaigns = campaigns
        self._populate_model()

    def _populate_model(self) -> None:
        sel = self.ui.campaign_list.selectionModel()
        sel.blockSignals(True)
        self._model.removeRows(0, self._model.rowCount())
        for c in self._sorted_campaigns():
            item = QStandardItem(c.name)
            item.setData(c, Qt.ItemDataRole.UserRole)
            if c.location:
                tip = f"📍 {c.location.latitude:.4f}, {c.location.longitude:.4f}"
            else:
                tip = "📋 Species list"
            item.setData(tip, Qt.ItemDataRole.ToolTipRole)
            self._model.appendRow(item)
        sel.blockSignals(False)

    def _sorted_campaigns(self) -> list[Campaign]:
        if self._sort_order == CampaignSortOrder.NAME_ASC:
            return sorted(self._campaigns, key=lambda c: c.name.lower())
        if self._sort_order == CampaignSortOrder.NAME_DESC:
            return sorted(self._campaigns, key=lambda c: c.name.lower(), reverse=True)
        if self._sort_order == CampaignSortOrder.DATE_MODIFIED_ASC:
            # AppState.campaigns is already mtime-descending, so ascending is
            # just the reverse, no extra filesystem stat() calls needed.
            return list(reversed(self._campaigns))
        return list(self._campaigns)

    # new / selection

    def _on_new(self) -> None:
        if self._list_locked:
            return
        if not self._confirm_stop_watching_if_busy():
            return
        self._detail.request_shutdown()  # no-op when idle
        self.ui.campaign_list.clearSelection()
        self._detail.open_new(self._existing_names())

    def _on_selection_changed(self, current, previous) -> None:
        if self._reverting_selection:
            return
        new_campaign = self._campaign_at(current)
        if new_campaign is not None and self._detail.is_busy():
            if not self._confirm_stop_watching():
                self._reverting_selection = True
                try:
                    self.ui.campaign_list.setCurrentIndex(previous)
                finally:
                    self._reverting_selection = False
                return
            self._detail.request_shutdown()

        if new_campaign is None:
            self._detail.show_empty()
        else:
            self._open_view(new_campaign)

    def _open_view(self, campaign: Campaign) -> None:
        self._detail.open_view(
            campaign,
            self._existing_names(),
            self._species_text_for(campaign),
            self._must_have_text_for(campaign),
        )

    def _open_edit(self, campaign: Campaign) -> None:
        self._detail.open_edit(
            campaign,
            self._existing_names(),
            self._species_text_for(campaign),
            self._must_have_text_for(campaign),
        )

    def _species_text_for(self, campaign: Campaign) -> str:
        if campaign.species_filter_mode != FilterMode.LIST:
            return ""
        return self._service.read_species_list(campaign)

    def _must_have_text_for(self, campaign: Campaign) -> str:
        if campaign.species_filter_mode != FilterMode.LOCATION:
            return ""
        return self._service.read_must_have_species(campaign)

    def _on_create_requested(
        self,
        name: str,
        mode: FilterMode,
        location: LatLon | None,
        species_text: str,
        must_have_text: str,
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
        elif mode == FilterMode.LOCATION:
            self._service.write_must_have_species(campaign, must_have_text)
        self._app_state.refresh_campaigns()
        self._select_by_name(name)

    def _on_update_requested(
        self,
        existing: Campaign,
        new_name: str,
        mode: FilterMode,
        location: LatLon | None,
        species_text: str,
        must_have_text: str,
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
        elif mode == FilterMode.LOCATION:
            self._service.write_must_have_species(updated, must_have_text)
        self._app_state.refresh_campaigns()
        self._select_by_name(new_name)

    def _on_delete_requested(self, campaign: Campaign) -> None:
        self._service.delete(campaign)
        self._app_state.refresh_campaigns()
        self._detail.show_empty()

    def _on_detail_cancelled(self) -> None:
        # Cancel closes the detail view and deselects the list item,
        # returning to the empty page. Re-clicking the same item re-opens
        # the view page via _on_list_clicked.
        self.ui.campaign_list.clearSelection()
        self._detail.show_empty()

    def _on_list_clicked(self, index) -> None:
        # Re-open the view page when the user clicks on an already-selected
        # item (currentChanged doesn't fire in that case).
        campaign = self._campaign_at(index)
        if campaign is not None:
            self._open_view(campaign)

    # context menu & keyboard shortcuts

    def _on_context_menu(self, pos) -> None:
        index = self.ui.campaign_list.indexAt(pos)
        campaign = self._campaign_at(index)
        menu = QMenu(self)
        if campaign is not None:
            menu.addAction("Edit").triggered.connect(lambda: self._open_edit(campaign))
            menu.addAction("Rename…").triggered.connect(lambda: self._rename_campaign(campaign))
            menu.addAction("Open Campaign Folder").triggered.connect(
                lambda: self._open_campaign_folder(campaign)
            )
            menu.addAction("Delete…").triggered.connect(lambda: self._show_delete_confirm(campaign))
            menu.addSeparator()
        menu.addMenu(self._build_sort_menu(menu))
        menu.exec(self.ui.campaign_list.viewport().mapToGlobal(pos))

    def _build_sort_menu(self, parent: QMenu) -> QMenu:
        submenu = QMenu("Sort Campaign List By", parent)
        group = QActionGroup(submenu)
        group.setExclusive(True)
        for order, label in SORT_ORDER_LABELS.items():
            action = submenu.addAction(label)
            action.setCheckable(True)
            action.setChecked(order == self._sort_order)
            action.triggered.connect(lambda _checked=False, o=order: self._set_sort_order(o))
            group.addAction(action)
        return submenu

    def _set_sort_order(self, order: CampaignSortOrder) -> None:
        if order == self._sort_order:
            return
        self._sort_order = order
        self._settings.campaign_sort_order = order.value
        selected = self._selected_campaign()
        self._populate_model()
        if selected is not None:
            self._select_by_name(selected.name)

    def _open_campaign_folder(self, campaign: Campaign) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(campaign.folder)))

    def _on_delete_shortcut(self) -> None:
        if self._list_locked:
            return
        campaign = self._selected_campaign()
        if campaign:
            self._show_delete_confirm(campaign)

    def _on_rename_shortcut(self) -> None:
        if self._list_locked:
            return
        campaign = self._selected_campaign()
        if campaign:
            self._rename_campaign(campaign)

    def _show_delete_confirm(self, campaign: Campaign) -> None:
        audio_count = self._service.count_audio_files(campaign)
        self._detail.show_delete_confirm(
            campaign,
            audio_count,
            existing_names=self._existing_names(),
            species_text=self._species_text_for(campaign),
            must_have_text=self._must_have_text_for(campaign),
        )

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

    # busy/cancel API (used by MainWindow's cancel-on-switch gate)

    def is_busy(self) -> bool:
        return self._detail.is_busy()

    def busy_label(self) -> str | None:
        return self._detail.busy_label()

    def request_shutdown(self) -> None:
        self._detail.request_shutdown()

    def _confirm_stop_watching_if_busy(self) -> bool:
        if not self._detail.is_busy():
            return True
        return self._confirm_stop_watching()

    def _confirm_stop_watching(self) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Switch campaign?")
        label = self._detail.busy_label() or "background task"
        box.setText(f"An {label} is running. Switching campaigns will stop it.")
        switch_btn = box.addButton("Switch campaign", QMessageBox.ButtonRole.DestructiveRole)
        keep_btn = box.addButton("Keep running", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep_btn)
        box.exec()
        return box.clickedButton() is switch_btn

    def _set_list_locked(self, locked: bool) -> None:
        self._list_locked = locked
        self.ui.campaign_list.setEnabled(not locked)
        self.ui.new_button.setEnabled(not locked)

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
