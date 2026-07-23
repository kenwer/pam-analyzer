"""Data + text formatting for the empty-state campaign overview.

The overview is rendered as a static rich-text block shown in a QLabel. Counts,
sizes, and date ranges all come straight from the cached AudioInventory (see
audio_inventory_discovery.py), which aggregates them bottom-up from week to
card to campaign when the inventory is built, not when this is rendered.
"""

import html
from dataclasses import dataclass
from datetime import datetime

from ...domain import CampaignInventory, merge_date_ranges
from .audio_inventory_tree_model import format_bytes

_INDENT = "&nbsp;" * 4
_ARU_COLOR = "#666"
_MUTED_COLOR = "#888"


@dataclass(frozen=True)
class CampaignOverviewEntry:
    name: str
    filter_text: str
    inventory: CampaignInventory | None  # None when no audio has been imported yet


def render_overview(entries: list[CampaignOverviewEntry]) -> tuple[str, str]:
    """Build (project_summary_html, campaigns_html) for the empty-state page."""
    summary_html = _format_project_summary(entries)
    campaigns_html = "".join(_campaign_block(entry) for entry in entries)
    return summary_html, campaigns_html


def _format_project_summary(entries: list[CampaignOverviewEntry]) -> str:
    """Rollup across all campaigns as labeled rows: counts, disk usage, date span."""
    if not entries:
        return ""
    inventories = [e.inventory for e in entries if e.inventory is not None]
    aru_count = sum(len(inv.cards) for inv in inventories)
    file_count = sum(inv.file_count for inv in inventories)
    dates = _format_range(merge_date_ranges(inv.date_range for inv in inventories))

    rows = [("Campaigns", f"{len(entries):,}")]
    if aru_count:
        rows.append(("ARUs", f"{aru_count:,}"))
    rows.append(("Recordings", f"{file_count:,}"))
    rows.append(("Disk usage", format_bytes(sum(inv.total_bytes for inv in inventories))))
    if dates:
        rows.append(("Date range", dates))

    row_html = "".join(
        f"<tr><td style='padding-right:1em'>{label}</td>"
        f"<td><b>{value}</b></td></tr>"
        for label, value in rows
    )
    return f"<table cellspacing='0' cellpadding='0'>{row_html}</table>"


def _campaign_block(entry: CampaignOverviewEntry) -> str:
    inv = entry.inventory
    name = f"<b>{html.escape(entry.name)}</b>"
    suffix = f" &middot; {html.escape(entry.filter_text)}" if entry.filter_text else ""
    if inv is None or inv.file_count == 0:
        body = f"{name} &nbsp; <span style='color:{_MUTED_COLOR}'>no audio imported</span>{suffix}"
        return f"<p style='margin:0 0 6px 0'>{body}</p>"

    head = f"{name} &nbsp; {_stats(inv.file_count, inv.total_bytes, inv.date_range)}{suffix}"
    lines = [head]
    for card in inv.cards:
        stats = _stats(card.file_count, card.total_bytes, card.date_range)
        lines.append(
            f"<span style='color:{_ARU_COLOR}'>{_INDENT}{html.escape(card.name)} &nbsp; {stats}</span>"
        )
    return f"<p style='margin:0 0 6px 0'>{'<br>'.join(lines)}</p>"


def _stats(file_count: int, total_bytes: int, rng: tuple[datetime, datetime] | None) -> str:
    unit = "file" if file_count == 1 else "files"
    text = f"{file_count:,} {unit} &middot; {format_bytes(total_bytes)}"
    dates = _format_range(rng)
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
