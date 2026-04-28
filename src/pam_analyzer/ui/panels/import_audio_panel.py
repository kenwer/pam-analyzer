"""Panel for importing audio from SD cards into campaign folders."""

from enum import Enum
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtWidgets import QDialog, QTableWidgetItem, QWidget

from ...domain.audio_import import (
    CardImportResult,
    CardQueue,
    ConflictChoice,
    DetectedCard,
    ImportProgress,
)
from ...domain.enums import FilterMode
from ...infrastructure import AudioImporter, PsutilSdCardScanner
from ...workers.audio_import_worker import AudioImportWorker
from ..app_state import AppState
from ..dialogs.import_conflict_dialog import ImportConflictDialog
from .ui_import_audio_panel import Ui_ImportAudioPanel


class ImportState(Enum):
    IDLE = "idle"
    WATCHING = "watching"
    COPYING = "copying"


_HINT_IDLE = "Select a campaign, then start watching to import audio files when an SD card is inserted."
_HINT_WATCHING = "Once all imports are finished, stop watching to prevent unintended imports."


class ImportAudioPanel(QWidget):
    def __init__(
        self,
        app_state: AppState,
        import_service: AudioImporter,
        sdcard_scanner: PsutilSdCardScanner,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ui = Ui_ImportAudioPanel()
        self.ui.setupUi(self)

        self._app_state = app_state
        self._service = import_service
        self._scanner = sdcard_scanner
        self._state = ImportState.IDLE
        self._queue = CardQueue()
        self._campaign_dir: Path | None = None
        self._results: list[CardImportResult] = []
        self._current_card: DetectedCard | None = None

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(2000)

        self._thread: QThread | None = None
        self._worker: AudioImportWorker | None = None

        self._wire_signals()
        self._apply_state()
        self._render_project(app_state.project)

    def _wire_signals(self) -> None:
        self._app_state.projectChanged.connect(self._render_project)
        self._app_state.campaignsChanged.connect(self._rebuild_campaign_combo)
        self._poll_timer.timeout.connect(self._on_poll)
        self.ui.campaign_combo.currentIndexChanged.connect(self._on_campaign_changed)
        self.ui.watch_button.clicked.connect(self._on_watch_clicked)

    def _render_project(self, project: object) -> None:
        if project is None:
            self.ui.campaign_combo.blockSignals(True)
            self.ui.campaign_combo.clear()
            self.ui.campaign_combo.blockSignals(False)
            self._campaign_dir = None
            if self._state != ImportState.IDLE:
                self._stop_watching()
        else:
            self._rebuild_campaign_combo(self._app_state.campaigns)
        self._apply_state()

    def _rebuild_campaign_combo(self, campaigns: list) -> None:
        combo = self.ui.campaign_combo
        combo.blockSignals(True)
        combo.clear()
        if campaigns:
            for c in campaigns:
                combo.addItem(c.name, c.folder)
        combo.blockSignals(False)
        if combo.count() > 0:
            combo.setCurrentIndex(0)
            self._on_campaign_changed(0)
        else:
            self._campaign_dir = None
            self.ui.campaign_info_label.clear()
        self._apply_state()

    def _on_campaign_changed(self, index: int) -> None:
        if index < 0:
            self._campaign_dir = None
            self.ui.campaign_info_label.clear()
            return
        self._campaign_dir = self.ui.campaign_combo.itemData(index)
        campaign_name = self.ui.campaign_combo.itemText(index)
        campaign = self._campaign_by_name(campaign_name)
        self.ui.campaign_info_label.setText(self._campaign_info_text(campaign))
        # Allow same cards to be re-imported into the new campaign.
        self._queue.clear_seen()
        self._apply_state()

    def _campaign_by_name(self, name: str) -> object:
        for c in self._app_state.campaigns:
            if c.name == name:
                return c
        return None

    def _campaign_info_text(self, campaign: object) -> str:
        if campaign is None:
            return ""
        if campaign.species_filter_mode == FilterMode.LOCATION and campaign.location:  # type: ignore[union-attr]
            loc = campaign.location  # type: ignore[union-attr]
            ns = "N" if loc.latitude >= 0 else "S"
            ew = "E" if loc.longitude >= 0 else "W"
            return f"Location  {abs(loc.latitude):.2f}°{ns}, {abs(loc.longitude):.2f}°{ew}"
        return "Species list"

    def _on_watch_clicked(self) -> None:
        if self._state == ImportState.IDLE:
            self._start_watching()
        else:
            self._stop_watching()

    def _start_watching(self) -> None:
        self._queue.reset()
        self._state = ImportState.WATCHING
        self._poll_timer.start()
        self._apply_state()
        self._app_state.statusMessage.emit("Watching for SD cards...")

    def _stop_watching(self) -> None:
        self._poll_timer.stop()
        if self._worker is not None:
            self._worker.request_cancel()
        self._state = ImportState.IDLE
        self._apply_state()
        self._app_state.statusMessage.emit("Stopped watching.")

    def _on_poll(self) -> None:
        project = self._app_state.project
        if project is None:
            return
        cards = self._scanner.scan(project.sdcard_name_pattern)
        self._queue.offer(cards)
        self._update_queue_label()
        if self._state == ImportState.WATCHING and self._queue.pending:
            self._start_next()

    def _start_next(self) -> None:
        card = self._queue.pop()
        if card is None:
            return
        self._current_card = card
        self._update_queue_label()

        try:
            files = self._service.list_card_files(card.mountpoint)
        except Exception as exc:
            self._current_card = None
            self._append_result(
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
            if self._state == ImportState.WATCHING and self._queue.pending:
                self._start_next()
            return

        campaign_dir = self._campaign_dir / card.name  # type: ignore[operator]
        conflict_report = self._service.detect_conflicts(files, campaign_dir)

        resolutions: dict[str, ConflictChoice] = {}
        if conflict_report.conflicts:
            if self.ui.overwrite_check.isChecked():
                resolutions = {c.filename: ConflictChoice.REPLACE for c in conflict_report.conflicts}
            else:
                dialog = ImportConflictDialog(list(conflict_report.conflicts), self)
                if dialog.exec() == QDialog.DialogCode.Rejected:
                    self._current_card = None
                    if self._state == ImportState.WATCHING and self._queue.pending:
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
            service=self._service,
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
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        for sig in (self._worker.finished, self._worker.failed):
            sig.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)

        self._state = ImportState.COPYING
        self._apply_state()
        self._thread.start()

    def _on_progress(self, snap: ImportProgress) -> None:
        if snap.files_total > 0:
            self.ui.progress_bar.setRange(0, snap.files_total)
            self.ui.progress_bar.setValue(snap.files_done)
        self.ui.files_label.setText(f"{snap.files_done} / {snap.files_total} files")
        if snap.elapsed > 1 and snap.files_done > 0:
            remaining = snap.elapsed / snap.files_done * (snap.files_total - snap.files_done)
            mins, secs = divmod(int(remaining), 60)
            self.ui.eta_label.setText(f"{mins}m {secs:02d}s" if mins else f"{secs}s")

    def _on_finished(self, result: CardImportResult) -> None:
        self._teardown_worker()
        self._append_result(result)
        if self._state == ImportState.COPYING:
            self._state = ImportState.WATCHING
        if self._state == ImportState.WATCHING and self._queue.pending:
            self._start_next()
        else:
            self._apply_state()

    def _on_failed(self, message: str) -> None:
        current_card = self._current_card
        self._teardown_worker()
        if current_card is not None:
            self._append_result(
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
        if self._state == ImportState.COPYING:
            self._state = ImportState.WATCHING
        if self._state == ImportState.WATCHING and self._queue.pending:
            self._start_next()
        else:
            self._apply_state()

    def _teardown_worker(self) -> None:
        if self._thread is not None:
            self._thread.wait()
            self._thread.deleteLater()
            self._thread = None
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        self._current_card = None

    def _append_result(self, result: CardImportResult) -> None:
        self._results.append(result)
        row = self.ui.results_table.rowCount()
        self.ui.results_table.insertRow(row)
        mb = result.bytes_copied / 1e6
        mbs = mb / result.elapsed if result.elapsed else 0.0
        mins, secs = divmod(int(result.elapsed), 60)
        time_str = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
        status = result.error if result.error else "OK"

        for col, text in enumerate(
            [
                result.card.name,
                str(result.files_copied),
                str(result.files_skipped),
                f"{mb:.1f}",
                f"{mbs:.1f}",
                time_str,
                status,
            ]
        ):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.ui.results_table.setItem(row, col, item)

        self._update_summary_label()

    def _update_summary_label(self) -> None:
        ok = sum(1 for r in self._results if not r.error)
        err = sum(1 for r in self._results if r.error)
        parts: list[str] = []
        if ok:
            parts.append(f"{ok} copied")
        if err:
            parts.append(f"{err} {'error' if err == 1 else 'errors'}")
        self.ui.summary_label.setText("  ".join(parts))

    def _update_queue_label(self) -> None:
        pending = self._queue.pending
        if pending:
            names = ", ".join(c.name for c in pending[:6])
            if len(pending) > 6:
                names += f" (+{len(pending) - 6} more)"
            self.ui.queue_label.setText(f"Next: {names}")
        else:
            self.ui.queue_label.clear()

    def _apply_state(self) -> None:
        project = self._app_state.project
        has_campaign = self._campaign_dir is not None and project is not None
        is_watching = self._state in (ImportState.WATCHING, ImportState.COPYING)
        is_copying = self._state == ImportState.COPYING

        self.ui.watch_button.setEnabled(has_campaign or is_watching)
        self.ui.watch_button.setText("Stop watching" if is_watching else "Start watching")
        self.ui.campaign_combo.setEnabled(self._state == ImportState.IDLE)
        self.ui.overwrite_check.setEnabled(not is_copying)
        self.ui.clear_check.setEnabled(not is_copying)
        self.ui.copying_widget.setVisible(is_copying)
        self.ui.hint_label.setText(_HINT_WATCHING if is_watching else _HINT_IDLE)

    def request_shutdown(self) -> None:
        """Called from closeEvent: cancel any running import and wait."""
        if self._worker is not None:
            self._worker.request_cancel()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
