"""How ImportOrchestrator continues or ends a queue after each card."""

from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from pam_analyzer.domain import Campaign, FilterMode
from pam_analyzer.domain.audio_import import ConflictChoice, DetectedCard, ImportSource, birdnet_week
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
    assert card_b_thread is not None
    orchestrator.stop_watching()
    card_b_thread.wait()


_STAMP = "20260619_073000"


def _flac_card(root: Path, name: str, seed: int) -> DetectedCard:
    folder = root / name
    folder.mkdir()
    audio = np.random.default_rng(seed).integers(-2000, 2000, 4800, dtype=np.int16)
    sf.write(folder / f"{_STAMP}.flac", audio, 48_000, subtype="PCM_16", format="FLAC")
    return DetectedCard(name=name, mountpoint=folder, device="", source=ImportSource.FOLDER)


def _already_imported(campaign: Campaign, card: str, seed: int) -> Path:
    """A different recording under the same name, where the import would write it."""
    week = campaign.folder / card / f"week_{birdnet_week(datetime(2026, 6, 19)):02d}"
    week.mkdir(parents=True)
    path = week / f"{_STAMP}.flac"
    audio = np.random.default_rng(seed).integers(-2000, 2000, 9600, dtype=np.int16)
    sf.write(path, audio, 48_000, subtype="PCM_16", format="FLAC")
    return path


def test_a_conflicting_card_waits_and_skipping_it_moves_on_to_the_next(
    qtbot, campaign: Campaign, tmp_path: Path
) -> None:
    existing = _already_imported(campaign, "CardA", seed=9)
    kept = existing.read_bytes()
    orchestrator = ImportOrchestrator(AudioImporter(), FakeScanner())
    results: list[str] = []
    orchestrator.result_ready.connect(lambda r: results.append(r.card.name))

    with qtbot.waitSignal(orchestrator.conflict_detected) as conflict:
        orchestrator.start_folder_import(
            campaign, [_flac_card(tmp_path, "CardA", seed=1), _flac_card(tmp_path, "CardB", seed=2)]
        )
    assert conflict.args[0].name == "CardA"
    assert orchestrator.is_busy()

    with qtbot.waitSignal(orchestrator.folder_import_stopped):
        orchestrator.skip_card()

    assert results == ["CardB"]
    assert existing.read_bytes() == kept
    assert (campaign.folder / "CardB").is_dir()


def test_replace_overwrites_the_conflicting_file(qtbot, campaign: Campaign, tmp_path: Path) -> None:
    existing = _already_imported(campaign, "CardA", seed=9)
    card = _flac_card(tmp_path, "CardA", seed=1)
    orchestrator = ImportOrchestrator(AudioImporter(), FakeScanner())

    with qtbot.waitSignal(orchestrator.conflict_detected):
        orchestrator.start_folder_import(campaign, [card])
    with qtbot.waitSignal(orchestrator.folder_import_stopped):
        orchestrator.resolve_conflict({f"{_STAMP}.flac": ConflictChoice.REPLACE})

    assert existing.read_bytes() == (card.mountpoint / f"{_STAMP}.flac").read_bytes()


def test_the_overwrite_option_replaces_without_asking(qtbot, campaign: Campaign, tmp_path: Path) -> None:
    existing = _already_imported(campaign, "CardA", seed=9)
    card = _flac_card(tmp_path, "CardA", seed=1)
    orchestrator = ImportOrchestrator(AudioImporter(), FakeScanner())
    orchestrator.set_options(overwrite=True, clear_after=False)
    asked: list[object] = []
    orchestrator.conflict_detected.connect(lambda *a: asked.append(a))

    with qtbot.waitSignal(orchestrator.folder_import_stopped):
        orchestrator.start_folder_import(campaign, [card])

    assert asked == []
    assert existing.read_bytes() == (card.mountpoint / f"{_STAMP}.flac").read_bytes()
