"""Data + text formatting for the empty-state campaign overview.

Instead of an interactive tree, the overview is rendered as a static rich-text
block shown in a QLabel: one bold campaign line with its aggregate stats, then
its ARUs indented beneath. Counts and sizes come straight from the cached
AudioInventory; date ranges are parsed best-effort from filenames and left
blank when a recorder doesn't stamp the time into names.
"""

import html
from dataclasses import dataclass
from datetime import datetime

from ...domain import CampaignInventory, date_range_from_stems
from .audio_inventory_tree_model import format_bytes

_INDENT = "&nbsp;" * 4
_ARU_COLOR = "#666"
_MUTED_COLOR = "#888"


@dataclass(frozen=True)
class CampaignOverviewEntry:
    name: str
    filter_text: str
    inventory: CampaignInventory | None  # None when no audio has been imported yet


def format_overview_html(entries: list[CampaignOverviewEntry]) -> str:
    """Render the campaigns and their ARUs as an indented rich-text outline."""
    return "".join(_campaign_block(entry) for entry in entries)


def _campaign_block(entry: CampaignOverviewEntry) -> str:
    inv = entry.inventory
    name = f"<b>{html.escape(entry.name)}</b>"
    suffix = f" &middot; {html.escape(entry.filter_text)}" if entry.filter_text else ""
    if inv is None or inv.file_count == 0:
        body = f"{name} &nbsp; <span style='color:{_MUTED_COLOR}'>no audio imported</span>{suffix}"
        return f"<p style='margin:0 0 6px 0'>{body}</p>"

    stems = (
        path.stem for card in inv.cards for week in card.weeks for path in week.files
    )
    head = f"{name} &nbsp; {_stats(inv.file_count, inv.total_bytes, stems)}{suffix}"
    lines = [head]
    for card in inv.cards:
        card_stems = (path.stem for week in card.weeks for path in week.files)
        stats = _stats(card.file_count, card.total_bytes, card_stems)
        lines.append(
            f"<span style='color:{_ARU_COLOR}'>{_INDENT}{html.escape(card.name)} &nbsp; {stats}</span>"
        )
    return f"<p style='margin:0 0 6px 0'>{'<br>'.join(lines)}</p>"


def _stats(file_count: int, total_bytes: int, stems) -> str:
    unit = "file" if file_count == 1 else "files"
    text = f"{file_count:,} {unit} &middot; {format_bytes(total_bytes)}"
    dates = _format_range(date_range_from_stems(stems))
    if dates:
        text += f" &middot; {dates}"
    return text


def _format_range(rng: tuple[datetime, datetime] | None) -> str:
    """Compact date range: single day, or 'start to end' collapsing a shared year."""
    if rng is None:
        return ""
    start, end = rng
    if start.date() == end.date():
        return start.strftime("%Y-%m-%d")
    if start.year == end.year:
        return f"{start:%Y-%m-%d} to {end:%m-%d}"
    return f"{start:%Y-%m-%d} to {end:%Y-%m-%d}"
