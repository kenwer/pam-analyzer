"""AudioImportWorker.run() eject-safety: only ever eject genuinely SD-sourced cards."""

from pathlib import Path

from pam_analyzer.domain.audio_import import CardImportResult, DetectedCard, ImportSource
from pam_analyzer.workers.audio_import_worker import AudioImportWorker


class FakeImporter:
    def __init__(self, result: CardImportResult) -> None:
        self._result = result

    def import_card(self, **_kwargs) -> CardImportResult:
        return self._result


class FakeScanner:
    def __init__(self) -> None:
        self.ejected: list[DetectedCard] = []

    def eject(self, card: DetectedCard) -> None:
        self.ejected.append(card)


def _run(card: DetectedCard, *, error: str = "") -> FakeScanner:
    result = CardImportResult(
        card=card,
        files_copied=1,
        files_skipped=0,
        bytes_copied=100,
        elapsed=0.1,
        error=error,
        dest_dir=Path("/campaign") / card.name,
    )
    scanner = FakeScanner()
    worker = AudioImportWorker(
        service=FakeImporter(result),
        scanner=scanner,
        card=card,
        files=[],
        dest_dir=Path("/campaign") / card.name,
        resolutions={},
        identical=(),
        clear_after=False,
    )
    worker.run()
    return scanner


def test_sd_card_is_ejected_on_success():
    card = DetectedCard(name="MSD-1", mountpoint=Path("/mnt/MSD-1"), device="/dev/disk4")
    scanner = _run(card)
    assert scanner.ejected == [card]


def test_folder_card_is_never_ejected_on_success():
    card = DetectedCard(
        name="OldRecordings", mountpoint=Path("/data/OldRecordings"), device="", source=ImportSource.FOLDER
    )
    scanner = _run(card)
    assert scanner.ejected == []


def test_sd_card_is_not_ejected_on_error():
    card = DetectedCard(name="MSD-1", mountpoint=Path("/mnt/MSD-1"), device="/dev/disk4")
    scanner = _run(card, error="disk full")
    assert scanner.ejected == []
