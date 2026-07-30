"""Tests for discover_audio_inventory: layout walk + size aggregation."""

from datetime import datetime
from pathlib import Path
from threading import Event

import pytest

from pam_analyzer.infrastructure import (
    discover_audio_inventory,
    discover_audio_structure,
    resolve_audio_sizes,
)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Lay out a realistic post-import audio tree:

    audio/
      alpha/                <- has campaign.toml
        MSD-X/
          week_01/  *.WAV (2 files)
          week_02/  *.WAV (1 file)
          CONFIG.TXT        (ignored)
        MSD-Y/
          *.WAV at root     (-> Unsorted bucket)
      beta/                 <- has campaign.toml but no cards yet
      stray/                <- no campaign.toml, ignored entirely
        anything.wav
    """
    audio = tmp_path / "audio"
    (audio / "alpha").mkdir(parents=True)
    (audio / "alpha" / "campaign.toml").write_text("", encoding="utf-8")

    msd_x = audio / "alpha" / "MSD-X"
    (msd_x / "week_01").mkdir(parents=True)
    (msd_x / "week_02").mkdir(parents=True)
    (msd_x / "week_01" / "20240101_120000.WAV").write_bytes(b"\x00" * 1024)
    (msd_x / "week_01" / "20240102_120000.WAV").write_bytes(b"\x00" * 2048)
    (msd_x / "week_02" / "20240108_120000.WAV").write_bytes(b"\x00" * 4096)
    (msd_x / "CONFIG.TXT").write_text("config", encoding="utf-8")

    msd_y = audio / "alpha" / "MSD-Y"
    msd_y.mkdir()
    (msd_y / "loose.wav").write_bytes(b"\x00" * 512)

    (audio / "beta").mkdir()
    (audio / "beta" / "campaign.toml").write_text("", encoding="utf-8")

    (audio / "stray").mkdir()
    (audio / "stray" / "anything.wav").write_bytes(b"\x00" * 100)

    return audio


def test_returns_empty_for_missing_root(tmp_path: Path):
    inv = discover_audio_inventory(tmp_path / "does-not-exist")
    assert inv.campaigns == ()


def test_only_dirs_with_campaign_toml_count_as_campaigns(project_dir: Path):
    inv = discover_audio_inventory(project_dir)
    names = sorted(c.name for c in inv.campaigns)
    assert names == ["alpha", "beta"]  # 'stray' has no campaign.toml -> excluded


def test_card_aggregation_and_week_buckets(project_dir: Path):
    inv = discover_audio_inventory(project_dir)
    alpha = inv.for_campaign("alpha")
    assert alpha is not None

    # Two cards under alpha: MSD-X and MSD-Y, in sorted order.
    assert [c.name for c in alpha.cards] == ["MSD-X", "MSD-Y"]

    # MSD-X: two week buckets, three WAVs total, CONFIG.TXT excluded.
    msd_x = alpha.cards[0]
    assert msd_x.file_count == 3
    assert msd_x.total_bytes == 1024 + 2048 + 4096
    assert [w.week for w in msd_x.weeks] == [1, 2]
    assert len(msd_x.weeks[0].files) == 2
    assert len(msd_x.weeks[1].files) == 1

    # MSD-Y: one loose .wav at root -> week=-1 bucket.
    msd_y = alpha.cards[1]
    assert msd_y.file_count == 1
    assert msd_y.weeks[0].week == -1


def test_empty_campaign_has_no_cards(project_dir: Path):
    inv = discover_audio_inventory(project_dir)
    beta = inv.for_campaign("beta")
    assert beta is not None
    assert beta.cards == ()
    assert beta.file_count == 0


def test_campaign_totals_sum_card_totals(project_dir: Path):
    inv = discover_audio_inventory(project_dir)
    alpha = inv.for_campaign("alpha")
    assert alpha is not None
    assert alpha.file_count == sum(c.file_count for c in alpha.cards)
    assert alpha.total_bytes == sum(c.total_bytes for c in alpha.cards)


def test_date_range_merges_bottom_up_from_filenames(project_dir: Path):
    """MSD-X's three files span 2024-01-01 to 2024-01-08; the campaign-level
    range should be merged from its cards without re-parsing any filenames
    at render time (see ui/models/campaign_overview.py)."""
    inv = discover_audio_inventory(project_dir)
    alpha = inv.for_campaign("alpha")
    assert alpha is not None

    msd_x = alpha.cards[0]
    assert msd_x.weeks[0].date_range == (datetime(2024, 1, 1, 12), datetime(2024, 1, 2, 12))
    assert msd_x.weeks[1].date_range == (datetime(2024, 1, 8, 12), datetime(2024, 1, 8, 12))
    assert msd_x.date_range == (datetime(2024, 1, 1, 12), datetime(2024, 1, 8, 12))

    # MSD-Y's loose file has no parseable timestamp.
    msd_y = alpha.cards[1]
    assert msd_y.date_range is None

    assert alpha.date_range == msd_x.date_range


def test_structure_has_counts_and_dates_but_no_sizes(project_dir: Path):
    """The structure phase reads everything a directory listing gives (counts,
    tree, date ranges) but leaves every byte total None for the size phase."""
    inv = discover_audio_structure(project_dir)
    alpha = inv.for_campaign("alpha")
    assert alpha is not None

    assert alpha.total_bytes is None
    assert alpha.file_count == 4  # counts do not need a stat
    assert alpha.date_range == (datetime(2024, 1, 1, 12), datetime(2024, 1, 8, 12))

    msd_x = alpha.cards[0]
    assert msd_x.total_bytes is None
    assert msd_x.file_count == 3
    assert msd_x.weeks[0].total_bytes is None
    assert msd_x.weeks[0].file_sizes is None

    assert inv.sizes_pending is True


def test_resolve_sizes_matches_full_discovery(project_dir: Path):
    """resolve_audio_sizes(structure) reproduces the one-shot discovery result."""
    resolved = resolve_audio_sizes(discover_audio_structure(project_dir))
    full = discover_audio_inventory(project_dir)

    assert resolved == full
    assert resolved.sizes_pending is False

    alpha = resolved.for_campaign("alpha")
    assert alpha is not None
    assert alpha.cards[0].total_bytes == 1024 + 2048 + 4096
    assert alpha.cards[0].weeks[0].file_sizes == (1024, 2048)


def test_resolve_sizes_cancelled_returns_pending_unchanged(project_dir: Path):
    """A cancelled stat pass returns the still-pending inventory, not a half-stat'd
    one, so a caller that switched projects can safely discard it."""
    structure = discover_audio_structure(project_dir)
    cancel = Event()
    cancel.set()

    result = resolve_audio_sizes(structure, cancel)

    assert result is structure  # unchanged, still pending
    assert result.sizes_pending is True


def test_only_empty_campaigns_are_not_pending(tmp_path: Path):
    """A project whose campaigns hold no audio has nothing to stat, so the
    structure phase gives each empty campaign a known byte total of 0 (not the
    pending None) and the project reads not pending, so no size worker runs.

    The 0 is what keeps None meaning exactly "pending": without it, an all-empty
    project would never run the size pass and would render "computing..." forever.
    """
    audio = tmp_path / "audio"
    (audio / "beta").mkdir(parents=True)
    (audio / "beta" / "campaign.toml").write_text("", encoding="utf-8")

    inv = discover_audio_structure(audio)

    beta = inv.for_campaign("beta")
    assert beta is not None
    assert beta.total_bytes == 0
    assert beta.size_pending is False
    assert inv.sizes_pending is False
