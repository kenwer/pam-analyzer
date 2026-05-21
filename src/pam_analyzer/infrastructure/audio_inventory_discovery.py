"""Walks audio_recordings_path to produce an AudioInventory snapshot.

The disk layout we discover is what AudioImporter writes:
  audio_recordings_path/
    <campaign>/                   # has campaign.toml
      <card>/                     # one subfolder per imported SD card
        week_NN/
          *.WAV
        CONFIG.TXT                # at card root, ignored here

Files outside any week_NN folder (e.g. drag-and-dropped manually) land in a
synthetic week=-1 bucket. The tree model renders that as 'Unsorted'.
"""

import logging
import os
import re
import time
from pathlib import Path

_log = logging.getLogger(__name__)

from ..domain import AudioInventory, CampaignInventory, CardInventory, WeekInventory
from . import paths

_AUDIO_SUFFIXES = paths.AUDIO_EXTENSIONS
_WEEK_DIRNAME = re.compile(r"^week_(\d{2})$")


def discover_audio_inventory(audio_root: Path) -> AudioInventory:
    """Build an AudioInventory from the filesystem under audio_root.

    Cheap by design: stats files for size but doesn't open any. Safe to call
    synchronously from the UI thread on project load and after each import.
    """
    if not audio_root.exists():
        return AudioInventory()

    dbg = _log.isEnabledFor(logging.DEBUG)
    campaigns: list[CampaignInventory] = []
    for sub in sorted(audio_root.iterdir()):
        if not sub.is_dir() or not paths.campaign_toml(sub).exists():
            continue
        t = time.perf_counter() if dbg else 0.0
        campaign = _inventory_for_campaign(sub)
        if dbg:
            _log.debug("audio_inventory: %s %.2fs (%d files)", sub.name, time.perf_counter() - t, campaign.file_count)
        campaigns.append(campaign)
    return AudioInventory(campaigns=tuple(campaigns))


def _inventory_for_campaign(campaign_dir: Path) -> CampaignInventory:
    cards: list[CardInventory] = []
    with os.scandir(campaign_dir) as it:
        subdirs = sorted((e for e in it if e.is_dir()), key=lambda e: e.name)
    for entry in subdirs:
        card = _inventory_for_card(entry)
        if card.file_count == 0:
            continue
        cards.append(card)

    file_count = sum(c.file_count for c in cards)
    total_bytes = sum(c.total_bytes for c in cards)
    return CampaignInventory(
        name=campaign_dir.name,
        folder=campaign_dir,
        cards=tuple(cards),
        file_count=file_count,
        total_bytes=total_bytes,
    )


def _inventory_for_card(card_entry: os.DirEntry) -> CardInventory:
    # Each value is a list of (path, size) pairs collected during the scan so
    # sizes are obtained from the same DirEntry that identified the file,
    # avoiding a second stat() call per file.
    by_week: dict[int, list[tuple[Path, int]]] = {}

    with os.scandir(card_entry.path) as it:
        for entry in it:
            if entry.is_dir():
                m = _WEEK_DIRNAME.match(entry.name)
                if m is None:
                    continue
                week = int(m.group(1))
                with os.scandir(entry.path) as week_it:
                    for file_entry in sorted(week_it, key=lambda e: e.name):
                        if _is_audio_entry(file_entry):
                            by_week.setdefault(week, []).append(
                                (Path(file_entry.path), _entry_size(file_entry))
                            )
            elif _is_audio_entry(entry):
                by_week.setdefault(-1, []).append(
                    (Path(entry.path), _entry_size(entry))
                )

    weeks: list[WeekInventory] = []
    for week_num in sorted(by_week):
        pairs = by_week[week_num]
        files = tuple(p for p, _ in pairs)
        sizes = tuple(s for _, s in pairs)
        weeks.append(WeekInventory(week=week_num, files=files, file_sizes=sizes, total_bytes=sum(sizes)))

    file_count = sum(len(w.files) for w in weeks)
    total_bytes = sum(w.total_bytes for w in weeks)
    return CardInventory(
        name=card_entry.name,
        folder=Path(card_entry.path),
        weeks=tuple(weeks),
        file_count=file_count,
        total_bytes=total_bytes,
    )


def _is_audio_entry(entry: os.DirEntry) -> bool:
    return entry.is_file() and os.path.splitext(entry.name)[1].lower() in _AUDIO_SUFFIXES


def _entry_size(entry: os.DirEntry) -> int:
    try:
        return entry.stat().st_size
    except OSError:
        return 0
