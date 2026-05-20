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

from enum import Enum
from pathlib import Path
from typing import Literal

from PySide6.QtCore import QCoreApplication, QSignalBlocker, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHeaderView,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from ...domain import AudioInventory, Campaign, FilterMode, LatLon
from ...domain.audio_import import (
    CardImportResult,
    CardQueue,
    ConflictChoice,
    DetectedCard,
    ImportProgress,
)
from ...infrastructure import AudioImporter, PsutilSdCardScanner
from ...widgets import MapPickerWidget
from ...workers.audio_import_worker import AudioImportWorker
from ..app_state import AppState
from ..dialogs.import_conflict_dialog import ImportConflictDialog
from ..models.audio_inventory_tree_model import AudioInventoryTreeModel, format_bytes
from .ui_campaign_detail_widget import Ui_CampaignDetailWidget

_Mode = Literal["empty", "view", "new", "edit", "confirm"]


class _ImportState(Enum):
    IDLE = "idle"
    WATCHING = "watching"
    COPYING = "copying"




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

    def __init__(
        self,
        app_state: AppState,
        import_service: AudioImporter,
        sdcard_scanner: PsutilSdCardScanner,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ui = Ui_CampaignDetailWidget()
        self.ui.setupUi(self)

        self._app_state = app_state
        self._import_service = import_service
        self._scanner = sdcard_scanner
        self._campaign: Campaign | None = None
        # Full list as received from the panel; open_edit derives a 'others'
        # set from it locally for uniqueness validation.
        self._existing_names: list[str] = []
        self._species_text: str = ""
        self._mode: _Mode = "empty"
        self._location_set = False

        # Import worker state, mirrored from the old ImportAudioPanel.
        self._import_state = _ImportState.IDLE
        self._queue = CardQueue()
        self._current_card: DetectedCard | None = None
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(2000)
        self._thread: QThread | None = None
        self._worker: AudioImportWorker | None = None

        self._map = MapPickerWidget()
        map_layout = QVBoxLayout(self.ui.map_container)
        map_layout.setContentsMargins(0, 0, 0, 0)
        map_layout.addWidget(self._map)

        self._setup_spinboxes()
        self._configure_view_page()
        self._wire_signals()
        self._apply_import_state()
        self.show_empty()

    def _setup_spinboxes(self) -> None:
        self.ui.lat_spin.setRange(-90.0, 90.0)
        self.ui.lat_spin.setDecimals(6)
        self.ui.lat_spin.setSingleStep(0.1)
        self.ui.lon_spin.setRange(-180.0, 180.0)
        self.ui.lon_spin.setDecimals(6)
        self.ui.lon_spin.setSingleStep(0.1)

    def _configure_view_page(self) -> None:
        """Hook up the runtime-only pieces of the view page (model, header,
        button clicks, initial stylesheet). The structure itself lives in
        campaign_detail_widget.ui.
        """
        self._inventory_model = AudioInventoryTreeModel(self)
        self.ui.inventory_tree.setModel(self._inventory_model)
        header = self.ui.inventory_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        self.ui.view_edit_button.clicked.connect(self._on_edit_clicked)
        self.ui.view_delete_button.clicked.connect(self._on_view_delete_clicked)
        self.ui.watch_button.clicked.connect(self._on_watch_clicked)

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
        self._poll_timer.timeout.connect(self._on_poll)

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
        self.ui.stack.setCurrentWidget(self.ui.view_page)
        self._refresh_inventory()
        self._apply_import_state()

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
        self.ui.view_name_label.setText(self._campaign.name)
        self.ui.view_filter_label.setText(self._filter_summary_text(self._campaign))

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
            self.ui.inventory_label.setText("Audio inventory:  (no files imported yet)")
            self._inventory_model.set_campaign(None)
            return
        n = campaign_inv.file_count
        size = format_bytes(campaign_inv.total_bytes)
        cards = len(campaign_inv.cards)
        self.ui.inventory_label.setText(
            f"Audio inventory:  {n:,} files  ·  {size}  ·  "
            f"{cards} card{'s' if cards != 1 else ''}"
        )
        self._inventory_model.set_campaign(campaign_inv)
        self.ui.inventory_tree.expandToDepth(0)

    # import lifecycle (formerly ImportAudioPanel)

    def _on_watch_clicked(self) -> None:
        if self._import_state == _ImportState.IDLE:
            self._start_watching()
        else:
            self._stop_watching()

    def _start_watching(self) -> None:
        if self._campaign is None:
            return
        self._queue.reset()
        self._import_state = _ImportState.WATCHING
        self._poll_timer.start()
        self._apply_import_state()
        self._app_state.importStarted.emit(self._campaign.name)
        self._app_state.statusMessage.emit(
            f"Watching for SD cards (campaign: {self._campaign.name})..."
        )

    def _stop_watching(self) -> None:
        self._poll_timer.stop()
        if self._worker is not None:
            self._worker.request_cancel()
        self._import_state = _ImportState.IDLE
        self._apply_import_state()
        self._app_state.importFinished.emit()
        self._app_state.statusMessage.emit("Stopped watching.")

    def _on_poll(self) -> None:
        project = self._app_state.project
        if project is None or self._campaign is None:
            return
        cards = self._scanner.scan(project.sdcard_name_pattern)
        self._queue.offer(cards)
        self._update_queue_label()
        if self._import_state == _ImportState.WATCHING and self._queue.pending:
            self._start_next()

    def _start_next(self) -> None:
        card = self._queue.pop()
        if card is None or self._campaign is None:
            return
        self._current_card = card
        self._update_queue_label()

        try:
            files = self._import_service.list_card_files(card.mountpoint)
        except Exception as exc:
            self._current_card = None
            self._app_state.append_import_result(
                CardImportResult(
                    card=card,
                    files_copied=0,
                    files_skipped=0,
                    bytes_copied=0,
                    elapsed=0.0,
                    error=str(exc),
                    dest_dir=None,
                )
            )
            if self._import_state == _ImportState.WATCHING and self._queue.pending:
                self._start_next()
            return

        campaign_dir = self._campaign.folder / card.name
        conflict_report = self._import_service.detect_conflicts(files, campaign_dir)

        resolutions: dict[str, ConflictChoice] = {}
        if conflict_report.conflicts:
            if self.ui.overwrite_check.isChecked():
                resolutions = {c.filename: ConflictChoice.REPLACE for c in conflict_report.conflicts}
            else:
                dialog = ImportConflictDialog(list(conflict_report.conflicts), self)
                if dialog.exec() == QDialog.DialogCode.Rejected:
                    self._current_card = None
                    if self._import_state == _ImportState.WATCHING and self._queue.pending:
                        self._start_next()
                    return
                resolutions = dialog.result_resolutions()

        self.ui.card_name_label.setText(card.name)
        self.ui.progress_bar.setRange(0, len(files) if files else 1)
        self.ui.progress_bar.setValue(0)
        self.ui.files_label.clear()
        self.ui.eta_label.clear()

        self._thread = QThread(self)
        self._worker = AudioImportWorker(
            service=self._import_service,
            scanner=self._scanner,
            card=card,
            files=files,
            dest_dir=campaign_dir,
            resolutions=resolutions,
            identical=conflict_report.identical,
            clear_after=self.ui.clear_check.isChecked(),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_import_progress)
        self._worker.finished.connect(self._on_import_finished)
        self._worker.failed.connect(self._on_import_failed)
        for sig in (self._worker.finished, self._worker.failed):
            sig.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)

        self._import_state = _ImportState.COPYING
        self._apply_import_state()
        self._thread.start()

    def _on_import_progress(self, snap: ImportProgress) -> None:
        if snap.files_total > 0:
            self.ui.progress_bar.setRange(0, snap.files_total)
            self.ui.progress_bar.setValue(snap.files_done)
        self.ui.files_label.setText(f"{snap.files_done} / {snap.files_total} files")
        if snap.elapsed > 1 and snap.files_done > 0:
            remaining = snap.elapsed / snap.files_done * (snap.files_total - snap.files_done)
            mins, secs = divmod(int(remaining), 60)
            self.ui.eta_label.setText(f"{mins}m {secs:02d}s" if mins else f"{secs}s")

    def _on_import_finished(self, result: CardImportResult) -> None:
        self._teardown_worker()
        self._app_state.append_import_result(result)
        if self._import_state == _ImportState.COPYING:
            self._import_state = _ImportState.WATCHING
        if self._import_state == _ImportState.WATCHING and self._queue.pending:
            self._start_next()
        else:
            self._apply_import_state()

    def _on_import_failed(self, message: str) -> None:
        current_card = self._current_card
        self._teardown_worker()
        if current_card is not None:
            self._app_state.append_import_result(
                CardImportResult(
                    card=current_card,
                    files_copied=0,
                    files_skipped=0,
                    bytes_copied=0,
                    elapsed=0.0,
                    error=message,
                    dest_dir=None,
                )
            )
        if self._import_state == _ImportState.COPYING:
            self._import_state = _ImportState.WATCHING
        if self._import_state == _ImportState.WATCHING and self._queue.pending:
            self._start_next()
        else:
            self._apply_import_state()

    def _teardown_worker(self) -> None:
        if self._thread is not None:
            self._thread.wait()
            self._thread.deleteLater()
            self._thread = None
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        self._current_card = None

    def _update_queue_label(self) -> None:
        pending = self._queue.pending
        if not pending:
            self.ui.queue_label.clear()
            return
        head = pending[:6]
        names = ", ".join(c.name for c in head)
        if len(pending) > 6:
            names += f", +{len(pending) - 6} more"
        n = len(pending)
        # Distinguish "1 card waiting" from "N cards waiting" so a multi-slot
        # reader's queue is immediately legible.
        if n == 1:
            self.ui.queue_label.setText(f"Next in queue: {names}")
        else:
            self.ui.queue_label.setText(f"{n} cards queued: {names}")

    def _apply_import_state(self) -> None:
        has_campaign = self._campaign is not None
        is_watching = self._import_state in (_ImportState.WATCHING, _ImportState.COPYING)
        is_copying = self._import_state == _ImportState.COPYING

        self.ui.watch_button.setEnabled(has_campaign or is_watching)
        self.ui.watch_button.setText("Stop SD import" if is_watching else "Start SD import")
        # The .ui's :checked stylesheet swaps to red while watching; we keep
        # the button's checked state in sync with the import state machine so
        # programmatic stops (e.g. request_shutdown) flip the visual too.
        if self.ui.watch_button.isChecked() != is_watching:
            self.ui.watch_button.setChecked(is_watching)
        self.ui.overwrite_check.setEnabled(not is_copying)
        self.ui.clear_check.setEnabled(not is_copying)
        for w in (self.ui.card_name_label, self.ui.progress_bar, self.ui.files_label, self.ui.eta_label):
            w.setVisible(is_copying)
        self.ui.import_hint_label.setText(
            "Once all imports are finished, stop the SD import to prevent unintended copies."
            if is_watching
            else "Start the SD import to copy audio files when a card is inserted."
        )

    def is_busy(self) -> bool:
        return self._import_state in (_ImportState.WATCHING, _ImportState.COPYING)

    def busy_label(self) -> str | None:
        if self._import_state == _ImportState.COPYING:
            return "audio import"
        if self._import_state == _ImportState.WATCHING:
            return "SD-card watcher"
        return None

    def request_shutdown(self) -> None:
        """Cancel any running import and wait. Drains queued worker signals so
        they're handled before the next session begins."""
        was_busy = self.is_busy()
        self._poll_timer.stop()
        if self._worker is not None:
            self._worker.request_cancel()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            QCoreApplication.processEvents()
        self._import_state = _ImportState.IDLE
        self._apply_import_state()
        if was_busy:
            self._app_state.importFinished.emit()

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
