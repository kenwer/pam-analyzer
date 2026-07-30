"""Unit tests for the empty-state overview text formatter."""

from pathlib import Path

from pam_analyzer.domain import (
    CampaignInventory,
    CardInventory,
    WeekInventory,
    date_range_from_stems,
    merge_date_ranges,
)
from pam_analyzer.ui.models.audio_inventory_tree_model import SIZE_PENDING
from pam_analyzer.ui.models.campaign_overview import CampaignOverviewEntry, render_overview


def _card(name: str, stamps: list[str]) -> CardInventory:
    files = tuple(Path(f"{s}.WAV") for s in stamps)
    date_range = date_range_from_stems(stamps)
    week = WeekInventory(
        week=1, files=files, total_bytes=1000, file_sizes=(1000,) * len(files), date_range=date_range
    )
    return CardInventory(
        name=name,
        folder=Path(name),
        weeks=(week,),
        file_count=len(files),
        total_bytes=1000,
        date_range=date_range,
    )


def _campaign(name: str, cards: tuple[CardInventory, ...]) -> CampaignInventory:
    return CampaignInventory(
        name=name,
        folder=Path(name),
        cards=cards,
        file_count=sum(c.file_count for c in cards),
        total_bytes=sum(c.total_bytes for c in cards),
        date_range=merge_date_ranges(c.date_range for c in cards),
    )


def test_format_lists_campaign_arus_and_dates():
    card = _card("ARU_01", ["20240501_060000", "20240503_061500"])
    entry = CampaignOverviewEntry(
        name="Meadow",
        filter_text="48.5210°N, 9.0576°E",
        inventory=_campaign("Meadow", (card,)),
    )
    _summary, html = render_overview([entry])
    assert "<b>Meadow</b>" in html
    assert "ARU_01" in html
    assert "2 files" in html
    assert "2024-05-01 to 05-03" in html
    assert "48.5210°N, 9.0576°E" in html


def test_format_singular_file_count():
    card = _card("ARU_01", ["20240501_060000"])
    entry = CampaignOverviewEntry(name="Solo", filter_text="", inventory=_campaign("Solo", (card,)))
    _summary, html = render_overview([entry])
    assert "1 file " in html
    assert "1 files" not in html


def test_format_campaign_without_audio():
    entry = CampaignOverviewEntry(name="Empty", filter_text="Species list", inventory=None)
    _summary, html = render_overview([entry])
    assert "<b>Empty</b>" in html
    assert "no audio imported" in html
    assert "Species list" in html


def test_format_blank_dates_without_timestamps():
    card = _card("ARU_X", ["recording001", "recording002"])
    entry = CampaignOverviewEntry(name="Odd", filter_text="", inventory=_campaign("Odd", (card,)))
    _summary, html = render_overview([entry])
    assert "2 files" in html
    assert " to " not in html  # no date range rendered


def test_format_empty_list_is_blank():
    assert render_overview([]) == ("", "")


def _pending_campaign(name: str, stamps: list[str]) -> CampaignInventory:
    """A campaign as it looks after the structure walk but before sizes land."""
    files = tuple(Path(f"{s}.WAV") for s in stamps)
    date_range = date_range_from_stems(stamps)
    week = WeekInventory(week=1, files=files, total_bytes=None, file_sizes=None, date_range=date_range)
    card = CardInventory(
        name="ARU_01", folder=Path("ARU_01"), weeks=(week,), file_count=len(files),
        total_bytes=None, date_range=date_range,
    )
    return CampaignInventory(
        name=name, folder=Path(name), cards=(card,), file_count=len(files),
        total_bytes=None, date_range=date_range,
    )


def test_pending_sizes_show_placeholder_and_omit_inline_bytes():
    entry = CampaignOverviewEntry(
        name="Meadow", filter_text="", inventory=_pending_campaign("Meadow", ["20240501_060000"])
    )
    summary, html = render_overview([entry])

    assert SIZE_PENDING in summary  # Disk usage row while the size pass runs
    assert "1 file" in html  # counts render immediately
    assert " B" not in html and "KB" not in html and "MB" not in html  # no byte figure yet


def _empty_campaign(name: str) -> CampaignInventory:
    """A created-but-not-yet-imported campaign: no files, size known to be 0.

    Discovery gives an empty campaign total_bytes=0 (not the pending None), since
    there is nothing to stat. See discover_audio_structure._build_campaign_inventory.
    """
    return CampaignInventory(
        name=name, folder=Path(name), cards=(), file_count=0, total_bytes=0, date_range=None
    )


def test_all_empty_project_totals_zero_not_pending():
    """An empty campaign is not size-pending, so a project whose campaigns are all
    empty shows a real 0 in the Disk usage row, never a stuck "computing..."
    (that project never runs a size pass, so a pending marker would be permanent)."""
    entries = [
        CampaignOverviewEntry(name=n, filter_text="", inventory=_empty_campaign(n))
        for n in ("alpha", "beta")
    ]
    summary, _html = render_overview(entries)

    assert SIZE_PENDING not in summary
    assert "0 B" in summary  # Disk usage rolls up to a known zero


def test_project_summary_merges_campaign_ranges():
    card_a = _card("ARU_A", ["20240101_060000"])
    card_b = _card("ARU_B", ["20240301_060000"])
    entries = [
        CampaignOverviewEntry(name="Alpha", filter_text="", inventory=_campaign("Alpha", (card_a,))),
        CampaignOverviewEntry(name="Beta", filter_text="", inventory=_campaign("Beta", (card_b,))),
    ]
    summary, _html = render_overview(entries)
    assert "2" in summary  # campaign count
    assert "2024-01-01 to 03-01" in summary  # merged across both campaigns
