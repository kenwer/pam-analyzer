"""Detail widget: form for creating, editing, and deleting a single campaign.

The widget owns form state only; it emits intent signals (createRequested,
updateRequested, deleteRequested) and lets CampaignsPanel orchestrate
service calls and error reporting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from PySide6.QtCore import QSignalBlocker, QTimer, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ...domain import AudioInventory, Campaign, FilterMode, LatLon
from ...widgets import MapPickerWidget
from ..app_state import AppState
from ..models.audio_inventory_tree_model import AudioInventoryTreeModel, format_bytes
from .ui_campaign_detail_widget import Ui_CampaignDetailWidget

_Mode = Literal["empty", "new", "edit", "confirm"]


class CampaignDetailWidget(QWidget):
    # name, mode, location|None, species_text
    createRequested = Signal(str, object, object, str)
    # existing campaign, new_name, mode, location|None, species_text
    updateRequested = Signal(object, str, object, object, str)
    # campaign to delete
    deleteRequested = Signal(object)
    cancelled = Signal()

    def __init__(self, app_state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ui = Ui_CampaignDetailWidget()
        self.ui.setupUi(self)

        self._app_state = app_state
        self._campaign: Campaign | None = None
        self._existing_names: set[str] = set()
        self._mode: _Mode = "empty"
        self._location_set = False

        self._map = MapPickerWidget()
        map_layout = QVBoxLayout(self.ui.map_container)
        map_layout.setContentsMargins(0, 0, 0, 0)
        map_layout.addWidget(self._map)

        self._setup_spinboxes()
        self._build_inventory_section()
        self._wire_signals()
        self.show_empty()

    def _setup_spinboxes(self) -> None:
        self.ui.lat_spin.setRange(-90.0, 90.0)
        self.ui.lat_spin.setDecimals(6)
        self.ui.lat_spin.setSingleStep(0.1)
        self.ui.lon_spin.setRange(-180.0, 180.0)
        self.ui.lon_spin.setDecimals(6)
        self.ui.lon_spin.setSingleStep(0.1)

    def _build_inventory_section(self) -> None:
        """Add a read-only audio inventory tree below the form, before the footer row.

        Lives inside form_page so it's visible while editing an existing
        campaign (mode='edit'). Hidden for mode='new' and on the non-form
        stack pages (empty/confirm).
        """
        self._inventory_label = QLabel(self.ui.form_page)
        self._inventory_model = AudioInventoryTreeModel(self)
        self._inventory_tree = QTreeView(self.ui.form_page)
        self._inventory_tree.setModel(self._inventory_model)
        self._inventory_tree.setRootIsDecorated(True)
        self._inventory_tree.setUniformRowHeights(True)
        self._inventory_tree.setAlternatingRowColors(True)
        header = self._inventory_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        layout = self.ui.form_layout
        # Insert just before the footer (last item) so the Save/Cancel buttons
        # keep their place at the bottom.
        insert_at = layout.count() - 1
        layout.insertWidget(insert_at, self._inventory_label)
        layout.insertWidget(insert_at + 1, self._inventory_tree)

    def _wire_signals(self) -> None:
        self._map.locationPicked.connect(self._on_map_location_picked)
        self.ui.lat_spin.valueChanged.connect(self._on_spinbox_changed)
        self.ui.lon_spin.valueChanged.connect(self._on_spinbox_changed)

        self.ui.mode_location_radio.toggled.connect(self._on_mode_toggled)

        self.ui.species_import_button.clicked.connect(self._on_import_species)
        self.ui.species_text.textChanged.connect(self._validate)

        self.ui.name_edit.textChanged.connect(self._validate)

        self.ui.save_button.clicked.connect(self._on_save)
        self.ui.cancel_button.clicked.connect(self.cancelled.emit)

        self.ui.delete_button.clicked.connect(self._on_delete)
        self.ui.confirm_cancel_button.clicked.connect(self.cancelled.emit)

        self._app_state.audioInventoryChanged.connect(self._on_audio_inventory_changed)

        _attach_text_drop_handler(self.ui.species_text, self._on_text_dropped)

    # state transitions

    def show_empty(self) -> None:
        self._mode = "empty"
        self._campaign = None
        self.ui.stack.setCurrentWidget(self.ui.empty_page)
        self._refresh_inventory()

    def open_new(self, existing_names: list[str]) -> None:
        self._mode = "new"
        self._campaign = None
        self._existing_names = set(existing_names)
        self._location_set = False
        self._reset_form(None, "")
        self._on_mode_toggled()
        self.ui.stack.setCurrentWidget(self.ui.form_page)
        QTimer.singleShot(0, self._map.clear)
        self.ui.name_edit.setFocus()
        self._refresh_inventory()

    def open_edit(
        self,
        campaign: Campaign,
        existing_names: list[str],
        species_text: str = "",
    ) -> None:
        self._mode = "edit"
        self._campaign = campaign
        self._existing_names = set(existing_names) - {campaign.name}
        self._location_set = campaign.location is not None
        self._reset_form(campaign, species_text)
        self._on_mode_toggled()
        self.ui.stack.setCurrentWidget(self.ui.form_page)

        if campaign.species_filter_mode == FilterMode.LOCATION and campaign.location:
            loc = campaign.location
            QTimer.singleShot(0, lambda: self._map.set_location(loc.latitude, loc.longitude))
        else:
            QTimer.singleShot(0, self._map.clear)
        self._refresh_inventory()

    def show_delete_confirm(self, campaign: Campaign, audio_count: int) -> None:
        self._mode = "confirm"
        self._campaign = campaign
        if audio_count == 0:
            msg = f'Delete campaign "{campaign.name}"?\nThis will remove the campaign folder.'
        else:
            msg = (
                f'Delete campaign "{campaign.name}"?\n'
                f"This will permanently delete the campaign folder and "
                f"{audio_count} audio file(s) inside it."
            )
        self.ui.confirm_label.setText(msg)
        self.ui.stack.setCurrentWidget(self.ui.confirm_page)

    # form helpers

    def _reset_form(self, campaign: Campaign | None, species_text: str) -> None:
        """Populate every field. campaign=None gives the 'new' defaults."""
        mode = campaign.species_filter_mode if campaign else FilterMode.LOCATION
        location = campaign.location if campaign else None
        with (
            QSignalBlocker(self.ui.name_edit),
            QSignalBlocker(self.ui.lat_spin),
            QSignalBlocker(self.ui.lon_spin),
            QSignalBlocker(self.ui.species_text),
        ):
            self.ui.name_edit.setText(campaign.name if campaign else "")
            self.ui.lat_spin.setValue(location.latitude if location else 0.0)
            self.ui.lon_spin.setValue(location.longitude if location else 0.0)
            self.ui.species_text.setPlainText(species_text)
            self.ui.species_label.clear()
            if mode == FilterMode.LOCATION:
                self.ui.mode_location_radio.setChecked(True)
            else:
                self.ui.mode_list_radio.setChecked(True)

    # event handlers

    def _on_map_location_picked(self, lat: float, lon: float) -> None:
        self._location_set = True
        with QSignalBlocker(self.ui.lat_spin), QSignalBlocker(self.ui.lon_spin):
            self.ui.lat_spin.setValue(lat)
            self.ui.lon_spin.setValue(lon)
        self._validate()

    def _on_spinbox_changed(self, _value: float) -> None:
        self._location_set = True
        self._map.set_location(self.ui.lat_spin.value(), self.ui.lon_spin.value())
        self._validate()

    def _on_mode_toggled(self) -> None:
        is_location = self.ui.mode_location_radio.isChecked()
        self.ui.location_group.setVisible(is_location)
        self.ui.species_group.setVisible(not is_location)
        self._validate()

    def _on_import_species(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import species list", "", "Text files (*.txt)")
        if path:
            self._load_species_file(Path(path))

    def _on_text_dropped(self, path: Path) -> None:
        self._load_species_file(path)

    def _load_species_file(self, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return
        self.ui.species_text.setPlainText(text)
        self.ui.species_label.setText(path.name)

    def _on_save(self) -> None:
        name = self.ui.name_edit.text().strip()
        is_loc = self.ui.mode_location_radio.isChecked()
        mode = FilterMode.LOCATION if is_loc else FilterMode.LIST
        location: LatLon | None = LatLon(self.ui.lat_spin.value(), self.ui.lon_spin.value()) if is_loc else None
        species_text = "" if is_loc else self.ui.species_text.toPlainText()

        if self._mode == "new":
            self.createRequested.emit(name, mode, location, species_text)
        elif self._mode == "edit" and self._campaign is not None:
            self.updateRequested.emit(self._campaign, name, mode, location, species_text)

    def _on_delete(self) -> None:
        if self._campaign is not None:
            self.deleteRequested.emit(self._campaign)

    # inventory display

    def _on_audio_inventory_changed(self, _inventory: AudioInventory) -> None:
        # Repaint the tree whenever the global inventory changes (e.g. after
        # an import finishes). We always re-query rather than diff because the
        # slice we display is small and the cost is negligible.
        self._refresh_inventory()

    def _refresh_inventory(self) -> None:
        if self._mode != "edit" or self._campaign is None:
            self._inventory_label.setVisible(False)
            self._inventory_tree.setVisible(False)
            self._inventory_model.set_campaign(None)
            return
        campaign_inv = self._app_state.audio_inventory.for_campaign(self._campaign.name)
        self._inventory_label.setVisible(True)
        self._inventory_tree.setVisible(True)
        if campaign_inv is None or campaign_inv.file_count == 0:
            self._inventory_label.setText("Audio inventory:  (no files imported yet)")
            self._inventory_model.set_campaign(None)
            return
        n = campaign_inv.file_count
        size = format_bytes(campaign_inv.total_bytes)
        cards = len(campaign_inv.cards)
        self._inventory_label.setText(
            f"Audio inventory:  {n:,} files  ·  {size}  ·  "
            f"{cards} card{'s' if cards != 1 else ''}"
        )
        self._inventory_model.set_campaign(campaign_inv)
        self._inventory_tree.expandToDepth(0)

    # validation

    def _validate(self) -> None:
        self.ui.save_button.setEnabled(self._is_valid())

    def _is_valid(self) -> bool:
        name = self.ui.name_edit.text().strip()
        if not name or "/" in name or "\\" in name:
            return False
        if name in self._existing_names:
            return False
        if self.ui.mode_location_radio.isChecked():
            return self._location_set
        return bool(self.ui.species_text.toPlainText().strip())


def _attach_text_drop_handler(edit: QPlainTextEdit, on_drop) -> None:
    """Wire drag-and-drop of a single .txt file onto a QPlainTextEdit."""
    edit.setAcceptDrops(True)

    def drag_enter(event):
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if len(urls) == 1 and urls[0].toLocalFile().endswith(".txt"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def drop(event):
        urls = event.mimeData().urls()
        if not urls:
            return
        path = Path(urls[0].toLocalFile())
        on_drop(path)
        event.acceptProposedAction()

    edit.dragEnterEvent = drag_enter  # type: ignore[method-assign]
    edit.dropEvent = drop  # type: ignore[method-assign]
