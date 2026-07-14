"""Unit tests for the empty-state overview text formatter."""

from pathlib import Path

from pam_analyzer.domain import CampaignInventory, CardInventory, WeekInventory
from pam_analyzer.ui.models.campaign_overview import (
    CampaignOverviewEntry,
    format_overview_html,
)


def _card(name: str, stamps: list[str]) -> CardInventory:
    files = tuple(Path(f"{s}.WAV") for s in stamps)
    week = WeekInventory(week=1, files=files, total_bytes=1000, file_sizes=(1000,) * len(files))
    return CardInventory(
        name=name, folder=Path(name), weeks=(week,), file_count=len(files), total_bytes=1000
    )


def _campaign(name: str, cards: tuple[CardInventory, ...]) -> CampaignInventory:
    return CampaignInventory(
        name=name,
        folder=Path(name),
        cards=cards,
        file_count=sum(c.file_count for c in cards),
        total_bytes=sum(c.total_bytes for c in cards),
    )


def test_format_lists_campaign_arus_and_dates():
    card = _card("ARU_01", ["20240501_060000", "20240503_061500"])
    entry = CampaignOverviewEntry(
        name="Meadow",
        filter_text="48.5210°N, 9.0576°E",
        inventory=_campaign("Meadow", (card,)),
    )
    html = format_overview_html([entry])
    assert "<b>Meadow</b>" in html
    assert "ARU_01" in html
    assert "2 files" in html
    assert "2024-05-01 to 05-03" in html
    assert "48.5210°N, 9.0576°E" in html


def test_format_singular_file_count():
    card = _card("ARU_01", ["20240501_060000"])
    entry = CampaignOverviewEntry(name="Solo", filter_text="", inventory=_campaign("Solo", (card,)))
    html = format_overview_html([entry])
    assert "1 file " in html
    assert "1 files" not in html


def test_format_campaign_without_audio():
    entry = CampaignOverviewEntry(name="Empty", filter_text="Species list", inventory=None)
    html = format_overview_html([entry])
    assert "<b>Empty</b>" in html
    assert "no audio imported" in html
    assert "Species list" in html


def test_format_blank_dates_without_timestamps():
    card = _card("ARU_X", ["recording001", "recording002"])
    entry = CampaignOverviewEntry(name="Odd", filter_text="", inventory=_campaign("Odd", (card,)))
    html = format_overview_html([entry])
    assert "2 files" in html
    assert " to " not in html  # no date range rendered


def test_format_empty_list_is_blank():
    assert format_overview_html([]) == ""
