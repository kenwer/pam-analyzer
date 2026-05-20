"""Detail widget: view, create, edit, and delete a single campaign.

States cycle through a QStackedWidget:
    empty   -> nothing selected
    view    -> show selected campaign (compact summary + audio inventory)
    new     -> form for a fresh campaign
    edit    -> form for an existing campaign (entered via Edit on view)
    confirm -> delete confirmation

The widget emits intent signals (createRequested, updateRequested,
deleteRequested) and lets CampaignsPanel orchestrate service calls. Cancel
from edit/confirm returns to view; cancel from new emits 'cancelled' so the
panel can clear the list selection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from PySide6.QtCore import QSignalBlocker, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ...domain import AudioInventory, Campaign, FilterMode, LatLon
from ...widgets import MapPickerWidget
from ..app_state import AppState
from ..models.audio_inventory_tree_model import AudioInventoryTreeModel, format_bytes
from .ui_campaign_detail_widget import Ui_CampaignDetailWidget

_Mode = Literal["empty", "view", "new", "edit", "confirm"]


class CampaignDetailWidget(QWidget):
    # name, mode, location|None, species_text
    createRequested = Signal(str, object, object, str)
    # existing campaign, new_name, mode, location|None, species_text
    updateRequested = Signal(object, str, object, object, str)
    # campaign to delete (after user confirmed on the confirm page)
    deleteRequested = Signal(object)
    # User clicked Delete on the view page; panel should fetch audio_count
    # and call show_delete_confirm to enter the confirm page.
    deleteConfirmRequested = Signal(object)
    # User backed out of new/confirm in a way that should drop the selection.
    cancelled = Signal()

    def __init__(self, app_state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ui = Ui_CampaignDetailWidget()
        self.ui.setupUi(self)

        self._app_state = app_state
        self._campaign: Campaign | None = None
        # Full list as received from the panel; open_edit derives a 'others'
        # set from it locally for uniqueness validation.
        self._existing_names: list[str] = []
        self._species_text: str = ""
        self._mode: _Mode = "empty"
        self._location_set = False

        self._map = MapPickerWidget()
        map_layout = QVBoxLayout(self.ui.map_container)
        map_layout.setContentsMargins(0, 0, 0, 0)
        map_layout.addWidget(self._map)

        self._setup_spinboxes()
        self._build_view_page()
        self._wire_signals()
        self.show_empty()

    def _setup_spinboxes(self) -> None:
        self.ui.lat_spin.setRange(-90.0, 90.0)
        self.ui.lat_spin.setDecimals(6)
        self.ui.lat_spin.setSingleStep(0.1)
        self.ui.lon_spin.setRange(-180.0, 180.0)
        self.ui.lon_spin.setDecimals(6)
        self.ui.lon_spin.setSingleStep(0.1)

    def _build_view_page(self) -> None:
        """Construct the view page (compact summary + inventory tree) and
        register it as a new page in the existing stack.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        # Header row: campaign name + Edit + Delete.
        header_row = QHBoxLayout()
        self._view_name_label = QLabel(page)
        name_font = self._view_name_label.font()
        name_font.setPointSizeF(name_font.pointSizeF() * 1.3)
        name_font.setWeight(QFont.Weight.Bold)
        self._view_name_label.setFont(name_font)
        header_row.addWidget(self._view_name_label, stretch=1)

        self._view_edit_button = QPushButton("Edit", page)
        self._view_edit_button.clicked.connect(self._on_edit_clicked)
        header_row.addWidget(self._view_edit_button)

        self._view_delete_button = QPushButton("Delete…", page)
        self._view_delete_button.clicked.connect(self._on_view_delete_clicked)
        header_row.addWidget(self._view_delete_button)

        layout.addLayout(header_row)

        # Filter summary line (location or species count).
        self._view_filter_label = QLabel(page)
        self._view_filter_label.setWordWrap(True)
        layout.addWidget(self._view_filter_label)

        # Inventory section.
        self._inventory_label = QLabel(page)
        self._inventory_label.setWordWrap(True)
        layout.addWidget(self._inventory_label)

        self._inventory_model = AudioInventoryTreeModel(self)
        self._inventory_tree = QTreeView(page)
        self._inventory_tree.setModel(self._inventory_model)
        self._inventory_tree.setRootIsDecorated(True)
        self._inventory_tree.setUniformRowHeights(True)
        self._inventory_tree.setAlternatingRowColors(True)
        header = self._inventory_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._inventory_tree, stretch=1)

        # Trailing spacer so a tiny inventory doesn't fight the tree for height.
        layout.addItem(QSpacerItem(0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding))

        self.ui.stack.addWidget(page)
        self._view_page = page

    def _wire_signals(self) -> None:
        self._map.locationPicked.connect(self._on_map_location_picked)
        self.ui.lat_spin.valueChanged.connect(self._on_spinbox_changed)
        self.ui.lon_spin.valueChanged.connect(self._on_spinbox_changed)

        self.ui.mode_location_radio.toggled.connect(self._on_mode_toggled)

        self.ui.species_import_button.clicked.connect(self._on_import_species)
        self.ui.species_text.textChanged.connect(self._validate)

        self.ui.name_edit.textChanged.connect(self._validate)

        self.ui.save_button.clicked.connect(self._on_save)
        self.ui.cancel_button.clicked.connect(self._on_form_cancel)

        self.ui.delete_button.clicked.connect(self._on_delete)
        self.ui.confirm_cancel_button.clicked.connect(self._on_confirm_cancel)

        self._app_state.audioInventoryChanged.connect(self._on_audio_inventory_changed)

        _attach_text_drop_handler(self.ui.species_text, self._on_text_dropped)

    # state transitions

    def show_empty(self) -> None:
        self._mode = "empty"
        self._campaign = None
        self.ui.stack.setCurrentWidget(self.ui.empty_page)

    def open_view(
        self,
        campaign: Campaign,
        existing_names: list[str],
        species_text: str = "",
    ) -> None:
        self._mode = "view"
        self._campaign = campaign
        self._existing_names = list(existing_names)
        self._species_text = species_text
        self._render_view()
        self.ui.stack.setCurrentWidget(self._view_page)
        self._refresh_inventory()

    def open_new(self, existing_names: list[str]) -> None:
        self._mode = "new"
        self._campaign = None
        self._existing_names = list(existing_names)
        self._species_text = ""
        self._location_set = False
        self._reset_form(None, "")
        self._on_mode_toggled()
        self.ui.stack.setCurrentWidget(self.ui.form_page)
        QTimer.singleShot(0, self._map.clear)
        self.ui.name_edit.setFocus()

    def open_edit(
        self,
        campaign: Campaign,
        existing_names: list[str],
        species_text: str = "",
    ) -> None:
        self._mode = "edit"
        self._campaign = campaign
        self._existing_names = list(existing_names)
        self._species_text = species_text
        self._location_set = campaign.location is not None
        self._reset_form(campaign, species_text)
        self._on_mode_toggled()
        self.ui.stack.setCurrentWidget(self.ui.form_page)

        if campaign.species_filter_mode == FilterMode.LOCATION and campaign.location:
            loc = campaign.location
            QTimer.singleShot(0, lambda: self._map.set_location(loc.latitude, loc.longitude))
        else:
            QTimer.singleShot(0, self._map.clear)

    def show_delete_confirm(
        self,
        campaign: Campaign,
        audio_count: int,
        existing_names: list[str] | None = None,
        species_text: str | None = None,
    ) -> None:
        self._mode = "confirm"
        self._campaign = campaign
        if existing_names is not None:
            self._existing_names = list(existing_names)
        if species_text is not None:
            self._species_text = species_text
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

    # view-mode handlers

    def _on_edit_clicked(self) -> None:
        if self._campaign is None:
            return
        self.open_edit(self._campaign, self._existing_names, self._species_text)

    def _on_view_delete_clicked(self) -> None:
        if self._campaign is not None:
            self.deleteConfirmRequested.emit(self._campaign)

    def _on_form_cancel(self) -> None:
        if self._mode == "edit" and self._campaign is not None:
            self.open_view(self._campaign, self._existing_names, self._species_text)
        else:
            # mode == "new": panel clears selection + shows empty.
            self.cancelled.emit()

    def _on_confirm_cancel(self) -> None:
        if self._campaign is not None:
            self.open_view(self._campaign, self._existing_names, self._species_text)
        else:
            self.cancelled.emit()

    def _render_view(self) -> None:
        if self._campaign is None:
            return
        self._view_name_label.setText(self._campaign.name)
        self._view_filter_label.setText(self._filter_summary_text(self._campaign))

    def _filter_summary_text(self, campaign: Campaign) -> str:
        if campaign.species_filter_mode == FilterMode.LOCATION and campaign.location is not None:
            loc = campaign.location
            ns = "N" if loc.latitude >= 0 else "S"
            ew = "E" if loc.longitude >= 0 else "W"
            return f"● Location  {abs(loc.latitude):.4f}°{ns}, {abs(loc.longitude):.4f}°{ew}"
        species_count = sum(1 for line in self._species_text.splitlines() if line.strip())
        if species_count:
            return f"● Species list  ·  {species_count} species"
        return "● Species list"

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
        # Repaint whenever the global inventory changes (e.g. after an import
        # finishes). We always re-query rather than diff because the slice we
        # display is small and the cost is negligible.
        self._refresh_inventory()

    def _refresh_inventory(self) -> None:
        if self._mode != "view" or self._campaign is None:
            self._inventory_model.set_campaign(None)
            return
        campaign_inv = self._app_state.audio_inventory.for_campaign(self._campaign.name)
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
        # In edit mode, the campaign's own name is allowed; in new mode it isn't.
        own_name = self._campaign.name if self._mode == "edit" and self._campaign is not None else None
        others = {n for n in self._existing_names if n != own_name}
        if name in others:
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
