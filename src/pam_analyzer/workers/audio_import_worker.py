"""Worker that copies one SD card in a background QThread."""

from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from ..domain.audio_import import ConflictChoice, DetectedCard, ImportSource, SdCardScanner
from ..infrastructure import AudioImporter


class AudioImportWorker(QObject):
    progress = Signal(object)   # ImportProgress
    finished = Signal(object)   # CardImportResult
    failed = Signal(str)        # unexpected exception message

    def __init__(
        self,
        service: AudioImporter,
        scanner: SdCardScanner,
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

    @Slot()
    def run(self) -> None:
        try:
            result = self._service.import_card(
                card=self._card,
                files=self._files,
                dest_dir=self._dest_dir,
                resolutions=self._resolutions,
                identical=self._identical,
                progress=self.progress.emit,
                is_cancelled=self.thread().isInterruptionRequested,
                clear_after=self._clear_after,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return

        if not result.error and self._card.source is ImportSource.SD_CARD:
            try:
                self._scanner.eject(self._card)
            except Exception:
                # Eject failure is a warning; the copy result stands as success.
                pass

        self.finished.emit(result)
