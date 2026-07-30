"""AppState.apply_resolved_audio_sizes: the second-phase size swap and its guard.

Project open applies a size-less audio inventory first; apply_loaded_project
then triggers resolve_pending_audio_sizes() itself, and a worker resolves
sizes and hands them back here. These tests cover that the swap fires
audioInventoryChanged, that a late result for a different folder is dropped,
and that the auto-trigger actually lands a fully sized inventory.
"""

from pathlib import Path

import pytest

from pam_analyzer.domain import Campaign, FilterMode, Project
from pam_analyzer.infrastructure import resolve_audio_sizes
from pam_analyzer.ui.app_state import AppState


@pytest.fixture
def project_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "proj"
    folder.mkdir()
    campaign = Campaign(name="alpha", folder=folder / "alpha", species_filter_mode=FilterMode.LOCATION)
    campaign.create()
    card = folder / "alpha" / "MSD-X" / "week_01"
    card.mkdir(parents=True)
    (card / "20240101_120000.WAV").write_bytes(b"\x00" * 4096)
    Project(folder=folder).save()
    return folder


@pytest.fixture
def state() -> AppState:
    # apply_loaded_project auto-triggers the size resolver whenever a loaded
    # project has audio, so a load_project() call below always launches a
    # real background worker. Drain it so it cannot outlive the test.
    s = AppState()
    yield s
    s.request_shutdown()


def test_open_leaves_sizes_pending_then_swap_resolves_them(
    qtbot, state: AppState, project_folder: Path, load_project
) -> None:
    load_project(state, project_folder)

    # The bundle applied a size-less inventory: counts are known, bytes are not.
    # (apply_loaded_project's own resolver is already running in the background,
    # but its queued result cannot land until the event loop is pumped below.)
    assert state.audio_inventory.sizes_pending is True

    sized = resolve_audio_sizes(state.audio_inventory)
    with qtbot.waitSignal(state.audioInventoryChanged):
        state.apply_resolved_audio_sizes(project_folder, sized)

    assert state.audio_inventory.sizes_pending is False
    assert state.audio_inventory.for_campaign("alpha").total_bytes == 4096


def test_resolve_pending_audio_sizes_runs_the_real_worker(
    qtbot, state: AppState, project_folder: Path, load_project
) -> None:
    """resolve_pending_audio_sizes (the project-open path) drives the same
    AudioInventoryRefresher as refresh_audio_inventory, just given the
    already-walked inventory so it skips straight to sizing."""
    load_project(state, project_folder)
    pending = state.audio_inventory
    assert pending.sizes_pending is True

    with qtbot.waitSignal(state.audioInventoryChanged, timeout=5000):
        state.resolve_pending_audio_sizes(project_folder, pending)

    assert state.audio_inventory.sizes_pending is False
    assert state.audio_inventory.for_campaign("alpha").total_bytes == 4096


def test_stale_result_for_other_folder_is_dropped(
    qtbot, state: AppState, project_folder: Path, load_project
) -> None:
    load_project(state, project_folder)
    pending_before = state.audio_inventory

    sized = resolve_audio_sizes(state.audio_inventory)
    # A late result from a project the user has since navigated away from.
    state.apply_resolved_audio_sizes(Path("/some/other/project"), sized)

    assert state.audio_inventory is pending_before  # unchanged, still pending
    assert state.audio_inventory.sizes_pending is True


def test_apply_loaded_project_auto_resolves_sizes(
    qtbot, state: AppState, project_folder: Path, load_project
) -> None:
    """apply_loaded_project itself triggers the size pass, so any caller that
    goes through it gets a fully sized inventory eventually without a
    separate follow-up call."""
    load_project(state, project_folder)

    qtbot.waitUntil(lambda: not state.audio_inventory.sizes_pending, timeout=5000)

    assert state.audio_inventory.for_campaign("alpha").total_bytes == 4096
