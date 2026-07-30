"""AudioInventoryRefresher and its AudioRefreshWorker (both in
audio_inventory_refresher.py).

The worker tests call run() directly. Its job is either the two-phase
walk-then-stat around discover_audio_structure/resolve_audio_sizes (no inventory
given), or just the stat phase on an inventory whose structure is already known
(project open, where load_project did the walk). The refresher tests
exercise the real QThread lifecycle it manages, so they wait on the queued
inventoryReady signal, which fires twice per from-scratch rebuild (size-less tree,
then fully sized) and once when given an already-walked inventory. The discovery
math itself is covered by tests/infrastructure/test_audio_inventory_discovery.py.
"""

from pathlib import Path

import pytest

from pam_analyzer.domain import AudioInventory, CampaignInventory
from pam_analyzer.workers.audio_inventory_refresher import (
    AudioInventoryRefresher,
    AudioRefreshWorker,
)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A minimal post-import tree: one campaign with one card holding two WAVs."""
    audio = tmp_path / "audio"
    card = audio / "alpha" / "MSD-X" / "week_01"
    card.mkdir(parents=True)
    (audio / "alpha" / "campaign.toml").write_text("", encoding="utf-8")
    (card / "20240101_120000.WAV").write_bytes(b"\x00" * 1024)
    (card / "20240102_120000.WAV").write_bytes(b"\x00" * 2048)
    return audio


def _pending_inventory() -> AudioInventory:
    # A campaign with no cards has nothing to stat: resolution fills its total to
    # 0 and clears sizes_pending without touching disk, so the test is fast.
    return AudioInventory(
        campaigns=(
            CampaignInventory(
                name="alpha", folder=Path("alpha"), cards=(), file_count=0, total_bytes=None,
                date_range=None,
            ),
        )
    )


def test_worker_run_emits_pending_tree_then_sized(qtbot, project_dir: Path) -> None:
    worker = AudioRefreshWorker(project_dir)

    received: list[tuple[str, Path, AudioInventory]] = []
    worker.structureReady.connect(lambda f, inv: received.append(("pending", f, inv)))
    worker.succeeded.connect(lambda f, inv: received.append(("sized", f, inv)))
    worker.run()

    assert [phase for phase, _, _ in received] == ["pending", "sized"]
    (_, pending_folder, pending_inv), (_, sized_folder, sized_inv) = received
    assert pending_folder == project_dir == sized_folder
    # Same structure both times; only the sizes differ.
    assert pending_inv.sizes_pending is True
    assert pending_inv.for_campaign("alpha").file_count == 2
    assert sized_inv.sizes_pending is False
    assert sized_inv.for_campaign("alpha").total_bytes == 3072


def test_worker_cancelled_run_emits_nothing(qtbot, project_dir: Path) -> None:
    worker = AudioRefreshWorker(project_dir)
    worker.cancel()

    received: list[object] = []
    worker.structureReady.connect(lambda *a: received.append(a))
    worker.succeeded.connect(lambda *a: received.append(a))
    worker.run()

    assert received == []


def test_worker_given_inventory_skips_structure_phase(qtbot) -> None:
    """Project open already walked the tree, so a pre-walked inventory should go
    straight to sizing: no structureReady, one succeeded."""
    folder = Path("/projects/demo")
    worker = AudioRefreshWorker(folder, _pending_inventory())

    received: list[tuple[str, Path, AudioInventory]] = []
    worker.structureReady.connect(lambda f, inv: received.append(("pending", f, inv)))
    worker.succeeded.connect(lambda f, inv: received.append(("sized", f, inv)))
    worker.run()

    assert [phase for phase, _, _ in received] == ["sized"]
    _, got_folder, got_inv = received[0]
    assert got_folder == folder
    assert got_inv.sizes_pending is False


def test_worker_given_inventory_cancelled_run_emits_nothing(qtbot) -> None:
    worker = AudioRefreshWorker(Path("/projects/demo"), _pending_inventory())
    worker.cancel()

    received: list[object] = []
    worker.succeeded.connect(lambda *a: received.append(a))
    worker.run()

    assert received == []


def test_refresh_emits_pending_then_sized(qtbot, project_dir: Path) -> None:
    refresher = AudioInventoryRefresher()

    received: list[AudioInventory] = []
    refresher.inventoryReady.connect(lambda _f, inv: received.append(inv))

    refresher.refresh(project_dir)
    qtbot.waitUntil(lambda: len(received) == 2, timeout=5000)

    assert received[0].sizes_pending is True
    assert received[1].sizes_pending is False
    assert received[1].for_campaign("alpha").total_bytes == 3072


def test_refresh_given_inventory_emits_sized_once(qtbot) -> None:
    refresher = AudioInventoryRefresher()
    folder = Path("/projects/demo")

    with qtbot.waitSignal(refresher.inventoryReady, timeout=5000) as blocker:
        refresher.refresh(folder, _pending_inventory())

    got_folder, got_inv = blocker.args
    assert got_folder == folder
    assert got_inv.sizes_pending is False


def test_second_refresh_supersedes_the_first(qtbot, project_dir: Path) -> None:
    """A refresh while one is in flight cancels it; the final sized result is the
    second call's. The sender-identity guard drops any late emit from the first."""
    refresher = AudioInventoryRefresher()

    sized: list[Path] = []
    refresher.inventoryReady.connect(
        lambda f, inv: sized.append(f) if not inv.sizes_pending else None
    )

    refresher.refresh(project_dir)
    refresher.refresh(project_dir)  # supersedes the first before it can finish
    qtbot.waitUntil(lambda: len(sized) >= 1, timeout=5000)
    # Let any stray late emit from the abandoned run flush through the event loop.
    qtbot.wait(50)

    assert sized == [project_dir]  # exactly one sized result, from the surviving run


def test_request_shutdown_is_safe_with_no_active_run(qtbot) -> None:
    refresher = AudioInventoryRefresher()
    refresher.request_shutdown()  # nothing running: must not raise
