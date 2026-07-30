"""Unit tests for the AudioInventory value-object transforms.

iter_files enumerates the tree's files. with_sizes rebuilds the tree from a
path-to-bytes map. Both are pure (no I/O): the size pass stats iter_files() and
hands the map back to with_sizes. These cover that transform in isolation.
"""

from pathlib import Path

from pam_analyzer.domain import AudioInventory, CampaignInventory, CardInventory, WeekInventory


def _week(week: int, *names: str) -> WeekInventory:
    return WeekInventory(
        week=week, files=tuple(Path(n) for n in names), total_bytes=None, date_range=None,
    )


def _pending() -> AudioInventory:
    """A two-card campaign as it looks after the structure walk: sizes pending."""
    card_x = CardInventory(
        name="MSD-X", folder=Path("MSD-X"),
        weeks=(_week(1, "a.wav", "b.wav"), _week(2, "c.wav")),
        file_count=3, total_bytes=None, date_range=None,
    )
    card_y = CardInventory(
        name="MSD-Y", folder=Path("MSD-Y"), weeks=(_week(-1, "loose.wav"),),
        file_count=1, total_bytes=None, date_range=None,
    )
    campaign = CampaignInventory(
        name="alpha", folder=Path("alpha"), cards=(card_x, card_y),
        file_count=4, total_bytes=None, date_range=None,
    )
    return AudioInventory(campaigns=(campaign,))


def test_iter_files_yields_every_file_across_the_tree():
    files = list(_pending().iter_files())
    assert [f.name for f in files] == ["a.wav", "b.wav", "c.wav", "loose.wav"]


def test_with_sizes_fills_totals_bottom_up():
    sizes = {Path("a.wav"): 1000, Path("b.wav"): 2000, Path("c.wav"): 4000, Path("loose.wav"): 500}
    sized = _pending().with_sizes(sizes)

    assert sized.sizes_pending is False
    alpha = sized.for_campaign("alpha")
    assert alpha.total_bytes == 7500  # sum of both cards
    assert alpha.cards[0].total_bytes == 7000  # MSD-X: 1000 + 2000 + 4000
    assert alpha.cards[0].weeks[0].file_sizes == (1000, 2000)
    assert alpha.cards[1].total_bytes == 500  # MSD-Y: loose file


def test_with_sizes_treats_missing_file_as_zero():
    """A file that could not be stat'd (dropped from the map) counts as 0 bytes,
    the same as the on-disk stat's OSError fallback."""
    sized = _pending().with_sizes({Path("a.wav"): 1000})  # only one of four known

    week = sized.for_campaign("alpha").cards[0].weeks[0]
    assert week.file_sizes == (1000, 0)


def test_with_sizes_is_pure_and_leaves_the_original_pending():
    pending = _pending()
    pending.with_sizes({Path("a.wav"): 1000})
    assert pending.sizes_pending is True  # original untouched (frozen value objects)
