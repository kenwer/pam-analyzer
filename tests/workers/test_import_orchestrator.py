"""How ImportOrchestrator continues or ends a queue after each card."""

from pathlib import Path

import pytest

from pam_analyzer.domain import Campaign, FilterMode
from pam_analyzer.infrastructure import AudioImporter
from pam_analyzer.workers import ImportOrchestrator
from tests.conftest import FakeScanner, ImporterUntilCancelled, folder_card


@pytest.fixture
def campaign(tmp_path: Path) -> Campaign:
    campaign = Campaign(name="alpha", folder=tmp_path / "alpha", species_filter_mode=FilterMode.LIST)
    campaign.create()
    return campaign


class _ListingFails(AudioImporter):
    def list_card_files(self, folder: Path) -> list[Path]:
        raise OSError("card unplugged")


def test_a_folder_batch_ends_when_its_last_card_cannot_be_listed(qtbot, campaign: Campaign, tmp_path: Path) -> None:
    orchestrator = ImportOrchestrator(_ListingFails(), FakeScanner())

    with qtbot.waitSignal(orchestrator.folder_import_stopped):
        orchestrator.start_folder_import(campaign, [folder_card(tmp_path, "CardA")])

    assert not orchestrator.is_busy()


def test_a_late_cancelled_card_does_not_stop_the_session_started_after_it(
    qtbot, campaign: Campaign, tmp_path: Path
) -> None:
    importer = ImporterUntilCancelled()
    orchestrator = ImportOrchestrator(importer, FakeScanner())
    orchestrator.start_folder_import(campaign, [folder_card(tmp_path, "CardA")])
    qtbot.waitUntil(lambda: importer.imported == ["CardA"])

    orchestrator.stop_watching()
    orchestrator.start_watching(campaign, "*")
    with qtbot.waitSignal(orchestrator.result_ready):
        pass

    assert orchestrator.is_busy()
    orchestrator.stop_watching()


def test_a_new_session_starts_its_card_once_the_cancelled_one_finishes(
    qtbot, campaign: Campaign, tmp_path: Path
) -> None:
    importer = ImporterUntilCancelled()
    scanner = FakeScanner()
    orchestrator = ImportOrchestrator(importer, scanner)
    orchestrator.start_folder_import(campaign, [folder_card(tmp_path, "CardA")])
    qtbot.waitUntil(lambda: importer.imported == ["CardA"])

    orchestrator.stop_watching()
    scanner.cards = [folder_card(tmp_path, "CardB")]
    orchestrator.start_watching(campaign, "*")
    orchestrator._on_poll()
    assert [c.name for c in orchestrator._queue.pending] == ["CardB"]

    qtbot.waitUntil(lambda: importer.imported == ["CardA", "CardB"])
    card_b_thread = orchestrator._thread
    orchestrator.stop_watching()
    card_b_thread.wait()
