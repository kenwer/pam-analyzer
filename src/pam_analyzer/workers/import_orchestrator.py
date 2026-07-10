"""Orchestrates SD card and manual-folder audio import: card detection, queue
processing, conflict negotiation, and copy worker lifecycle.

State machine:
    IDLE -> WATCHING: start_watching() or start_folder_import() called
    WATCHING -> COPYING: card ready, no conflicts (or overwrite enabled)
    WATCHING -> AWAITING_CONFLICT: card ready, conflicts present and overwrite disabled
    AWAITING_CONFLICT -> COPYING: resolve_conflict() called
    AWAITING_CONFLICT -> WATCHING: skip_card() called
    COPYING -> WATCHING: worker finished or failed
    WATCHING -> IDLE: queue drains after a start_folder_import() batch
    any busy state -> IDLE: stop_watching() or request_shutdown()

start_watching() arms the SD poll timer; start_folder_import() shares the same
queue-draining machinery but never starts the timer, so a folder batch cannot
be re-offered cards by the SD scanner. self._batch_source tracks which entry
point started the current run so the queue-drained tail (_on_worker_finished /
_on_worker_failed) knows whether to keep watching for a card (SD) or drop back
to IDLE (folder, a one-shot batch), and so busy_label() reports correctly.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, QTimer, Signal

from ..domain import Campaign
from ..domain.audio_import import (
    CardImportResult,
    CardQueue,
    ConflictChoice,
    DetectedCard,
    ImportSource,
)
from ..infrastructure import AudioImporter, PsutilSdCardScanner
from .audio_import_worker import AudioImportWorker


class _State(Enum):
    IDLE = "idle"
    WATCHING = "watching"
    AWAITING_CONFLICT = "awaiting_conflict"
    COPYING = "copying"


class ImportOrchestrator(QObject):
    watching_started = Signal(str)          # campaign name
    watching_stopped = Signal()
    folder_import_started = Signal(str)     # campaign name
    folder_import_stopped = Signal()
    card_started = Signal(object, int)      # DetectedCard, file_count
    progress = Signal(object)               # ImportProgress
    result_ready = Signal(object)           # CardImportResult
    conflict_detected = Signal(object, object)  # DetectedCard, ConflictReport
    queue_changed = Signal(list)            # list[DetectedCard]

    def __init__(
        self,
        importer: AudioImporter,
        scanner: PsutilSdCardScanner,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._importer = importer
        self._scanner = scanner
        self._campaign: Campaign | None = None
        self._pattern: str = ""
        self._overwrite: bool = False
        self._clear_after: bool = False
        self._state = _State.IDLE
        self._batch_source = ImportSource.SD_CARD
        self._queue = CardQueue()
        self._current_card: DetectedCard | None = None
        # Stored while AWAITING_CONFLICT, cleared on resolve or skip.
        self._pending_files: list[Path] | None = None
        self._pending_campaign_dir: Path | None = None
        self._pending_identical: tuple[str, ...] = ()
        self._thread: QThread | None = None
        self._worker: AudioImportWorker | None = None
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(2000)
        self._poll_timer.timeout.connect(self._on_poll)

    def start_watching(self, campaign: Campaign, pattern: str) -> None:
        self._campaign = campaign
        self._pattern = pattern
        self._batch_source = ImportSource.SD_CARD
        self._queue.reset()
        self._state = _State.WATCHING
        self._poll_timer.start()
        self.watching_started.emit(campaign.name)

    def start_folder_import(self, campaign: Campaign, cards: list[DetectedCard]) -> None:
        """Import a pre-confirmed batch of folder-sourced cards.

        Reuses the same queue/conflict/worker pipeline as SD watching, but
        never starts the poll timer, so the SD scanner cannot add cards to
        this batch. The queue drains once and the state returns to IDLE
        (see _on_worker_finished/_on_worker_failed), unlike SD watching,
        which stays armed for the next card indefinitely.
        """
        self._campaign = campaign
        self._batch_source = ImportSource.FOLDER
        self._queue.reset()
        self._queue.offer(cards)
        self._state = _State.WATCHING
        self.folder_import_started.emit(campaign.name)
        self.queue_changed.emit(list(self._queue.pending))
        self._start_next()

    def list_card_files(self, folder: Path) -> list[Path]:
        return self._importer.list_card_files(folder)

    def has_direct_audio(self, folder: Path) -> bool:
        return self._importer.has_direct_audio(folder)

    def stop_watching(self) -> None:
        """Cancel the current import session, SD-watching or folder batch alike."""
        self._poll_timer.stop()
        if self._worker is not None:
            self._worker.request_cancel()
        self._state = _State.IDLE
        if self._batch_source is ImportSource.FOLDER:
            self.folder_import_stopped.emit()
        else:
            self.watching_stopped.emit()

    def set_options(self, overwrite: bool, clear_after: bool) -> None:
        self._overwrite = overwrite
        self._clear_after = clear_after

    def resolve_conflict(self, resolutions: dict[str, ConflictChoice]) -> None:
        if self._state != _State.AWAITING_CONFLICT:
            return
        card = self._current_card
        files = self._pending_files
        campaign_dir = self._pending_campaign_dir
        identical = self._pending_identical
        self._pending_files = None
        self._pending_campaign_dir = None
        self._pending_identical = ()
        self._state = _State.COPYING
        self._launch_worker(card, files, campaign_dir, resolutions, identical)

    def skip_card(self) -> None:
        if self._state != _State.AWAITING_CONFLICT:
            return
        self._current_card = None
        self._pending_files = None
        self._pending_campaign_dir = None
        self._pending_identical = ()
        self._state = _State.WATCHING
        if self._queue.pending:
            self._start_next()
        elif self._batch_source is ImportSource.FOLDER:
            self._state = _State.IDLE
            self.queue_changed.emit([])
            self.folder_import_stopped.emit()
        else:
            self.queue_changed.emit([])

    def request_shutdown(self) -> None:
        """Cancel any running import and wait for the worker thread to finish.
        Drains queued worker signals so they are handled before the next session."""
        was_busy = self.is_busy()
        self._poll_timer.stop()
        if self._worker is not None:
            self._worker.request_cancel()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            QCoreApplication.processEvents()
        self._state = _State.IDLE
        if was_busy:
            if self._batch_source is ImportSource.FOLDER:
                self.folder_import_stopped.emit()
            else:
                self.watching_stopped.emit()

    def is_busy(self) -> bool:
        return self._state in (_State.WATCHING, _State.AWAITING_CONFLICT, _State.COPYING)

    def busy_label(self) -> str | None:
        if self._state == _State.COPYING:
            return "audio import"
        if self._state in (_State.WATCHING, _State.AWAITING_CONFLICT):
            return "folder import" if self._batch_source is ImportSource.FOLDER else "SD-card watcher"
        return None

    def _on_poll(self) -> None:
        if self._campaign is None or self._state != _State.WATCHING:
            return
        cards = self._scanner.scan(self._pattern)
        self._queue.offer(cards)
        self.queue_changed.emit(list(self._queue.pending))
        if self._queue.pending:
            self._start_next()

    def _start_next(self) -> None:
        card = self._queue.pop()
        if card is None or self._campaign is None:
            return
        self._current_card = card
        self.queue_changed.emit(list(self._queue.pending))

        try:
            files = self._importer.list_card_files(card.mountpoint)
        except Exception as exc:  # noqa: BLE001
            self._current_card = None
            self.result_ready.emit(
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
            if self._state == _State.WATCHING and self._queue.pending:
                self._start_next()
            return

        campaign_dir = self._campaign.folder / card.name
        conflict_report = self._importer.detect_conflicts(files, campaign_dir)

        if conflict_report.conflicts and not self._overwrite:
            self._pending_files = files
            self._pending_campaign_dir = campaign_dir
            self._pending_identical = conflict_report.identical
            self._state = _State.AWAITING_CONFLICT
            self.conflict_detected.emit(card, conflict_report)
            return

        resolutions: dict[str, ConflictChoice] = (
            {c.filename: ConflictChoice.REPLACE for c in conflict_report.conflicts}
            if conflict_report.conflicts
            else {}
        )
        self._state = _State.COPYING
        self._launch_worker(card, files, campaign_dir, resolutions, conflict_report.identical)

    def _launch_worker(
        self,
        card: DetectedCard,
        files: list[Path],
        dest_dir: Path,
        resolutions: dict[str, ConflictChoice],
        identical: tuple[str, ...],
    ) -> None:
        self.card_started.emit(card, len(files))
        self._thread = QThread(self)
        self._worker = AudioImportWorker(
            service=self._importer,
            scanner=self._scanner,
            card=card,
            files=files,
            dest_dir=dest_dir,
            resolutions=resolutions,
            identical=identical,
            clear_after=self._clear_after,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.progress)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.failed.connect(self._on_worker_failed)
        for sig in (self._worker.finished, self._worker.failed):
            sig.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)
        self._thread.start()

    def _on_worker_finished(self, result: CardImportResult) -> None:
        self._teardown_worker()
        self.result_ready.emit(result)
        if self._state == _State.COPYING:
            self._state = _State.WATCHING
        if self._state == _State.WATCHING and self._queue.pending:
            self._start_next()
        elif self._state == _State.WATCHING and self._batch_source is ImportSource.FOLDER:
            self._state = _State.IDLE
            self.folder_import_stopped.emit()

    def _on_worker_failed(self, message: str) -> None:
        current_card = self._current_card
        self._teardown_worker()
        if current_card is not None:
            self.result_ready.emit(
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
        if self._state == _State.COPYING:
            self._state = _State.WATCHING
        if self._state == _State.WATCHING and self._queue.pending:
            self._start_next()
        elif self._state == _State.WATCHING and self._batch_source is ImportSource.FOLDER:
            self._state = _State.IDLE
            self.folder_import_stopped.emit()

    def _teardown_worker(self) -> None:
        if self._thread is not None:
            self._thread.wait()
            self._thread.deleteLater()
            self._thread = None
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        self._current_card = None
