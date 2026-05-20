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

import re
from pathlib import Path

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

    campaigns: list[CampaignInventory] = []
    for sub in sorted(audio_root.iterdir()):
        if not sub.is_dir() or not paths.campaign_toml(sub).exists():
            continue
        campaigns.append(_inventory_for_campaign(sub))
    return AudioInventory(campaigns=tuple(campaigns))


def _inventory_for_campaign(campaign_dir: Path) -> CampaignInventory:
    cards: list[CardInventory] = []
    for sub in sorted(campaign_dir.iterdir()):
        if not sub.is_dir():
            continue
        card = _inventory_for_card(sub)
        if card.file_count == 0:
            continue  # empty card folder, ignore (likely incomplete import or stray dir)
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


def _inventory_for_card(card_dir: Path) -> CardInventory:
    by_week: dict[int, list[Path]] = {}
    for entry in card_dir.iterdir():
        if entry.is_dir():
            m = _WEEK_DIRNAME.match(entry.name)
            if m is None:
                continue
            week = int(m.group(1))
            files = [p for p in sorted(entry.iterdir()) if _is_audio(p)]
            if files:
                by_week.setdefault(week, []).extend(files)
        elif _is_audio(entry):
            by_week.setdefault(-1, []).append(entry)

    weeks: list[WeekInventory] = []
    for week_num in sorted(by_week):
        files = tuple(by_week[week_num])
        total = sum(_safe_size(p) for p in files)
        weeks.append(WeekInventory(week=week_num, files=files, total_bytes=total))

    file_count = sum(len(w.files) for w in weeks)
    total_bytes = sum(w.total_bytes for w in weeks)
    return CardInventory(
        name=card_dir.name,
        folder=card_dir,
        weeks=tuple(weeks),
        file_count=file_count,
        total_bytes=total_bytes,
    )


def _is_audio(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in _AUDIO_SUFFIXES


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
