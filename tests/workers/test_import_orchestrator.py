"""How ImportOrchestrator continues or ends a queue after each card."""

from pathlib import Path

import pytest

from pam_analyzer.domain import Campaign, FilterMode
from pam_analyzer.infrastructure import AudioImporter
from pam_analyzer.workers import ImportOrchestrator
from tests.conftest import FakeScanner, folder_card


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

