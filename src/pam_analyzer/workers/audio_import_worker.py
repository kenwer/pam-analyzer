"""Worker that copies one SD card in a background QThread."""

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from ..domain.audio_import import ConflictChoice, DetectedCard, ImportProgress
from ..infrastructure import AudioImporter, PsutilSdCardScanner


class _SignalProgress:
    """Forwards import_card progress callbacks to AudioImportWorker.progress signal."""

    def __init__(self, worker: "AudioImportWorker") -> None:
        self._worker = worker

    def __call__(self, snap: ImportProgress) -> None:
        self._worker.progress.emit(snap)


class AudioImportWorker(QObject):
    progress = Signal(object)   # ImportProgress
    finished = Signal(object)   # CardImportResult
    failed = Signal(str)        # unexpected exception message

    def __init__(
        self,
        service: AudioImporter,
        scanner: PsutilSdCardScanner,
        card: DetectedCard,
        files: list[Path],
        dest_dir: Path,
        resolutions: dict[str, ConflictChoice],
        identical: tuple[str, ...],
        clear_after: bool,
    ) -> None:
        super().__init__()
        self._service = service
        self._scanner = scanner
        self._card = card
        self._files = files
        self._dest_dir = dest_dir
        self._resolutions = resolutions
        self._identical = identical
        self._clear_after = clear_after
        self._cancel_event = threading.Event()

    @Slot()
    def run(self) -> None:
        prog = _SignalProgress(self)
        try:
            result = self._service.import_card(
                card=self._card,
                files=self._files,
                dest_dir=self._dest_dir,
                resolutions=self._resolutions,
                identical=self._identical,
                progress=prog,
                is_cancelled=self._cancel_event.is_set,
                clear_after=self._clear_after,
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return

        if not result.error:
            try:
                self._scanner.eject(self._card)
            except Exception:  # noqa: BLE001
                # Eject failure is a warning; the copy result stands as success.
                pass

        self.finished.emit(result)

    def request_cancel(self) -> None:
        self._cancel_event.set()
