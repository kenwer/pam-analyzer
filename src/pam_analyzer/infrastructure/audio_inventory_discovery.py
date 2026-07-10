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
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from ..domain import AudioInventory, CampaignInventory, CardInventory, WeekInventory
from . import paths

_log = logging.getLogger(__name__)

_AUDIO_SUFFIXES = paths.AUDIO_EXTENSIONS
_WEEK_DIRNAME = re.compile(r"^week_(\d{2})$")

# Caps how many filesystem round trips run concurrently. Listing a directory
# (scandir) is one round trip regardless of how many entries it returns, but
# on network shares like SMB the server may not report file-type (d_type) in
# that listing, so os.DirEntry.is_dir()/is_file() each silently fall back to
# their own round trip. Those per-entry checks, not the listing itself, are
# what a bounded thread pool here is parallelizing.
_MAX_STAT_WORKERS = 32


@dataclass
class _CardStructure:
    name: str
    folder: Path
    by_week: dict[int, list[Path]]


def discover_audio_inventory(audio_root: Path) -> AudioInventory:
    """Build an AudioInventory from the filesystem under audio_root.

    Directory structure and file sizes are both resolved with a bounded
    thread pool: on local disks the per-entry type/size checks are
    effectively free, but on network filesystems like SMB each one can be
    its own round trip, so doing thousands of them one at a time serializes
    on latency rather than throughput. Safe to call synchronously from the
    UI thread on project load and after each import.
    """
    if not audio_root.exists():
        return AudioInventory()

    dbg = _log.isEnabledFor(logging.DEBUG)
    t0 = time.perf_counter() if dbg else 0.0

    campaign_dirs = [
        sub for sub in sorted(audio_root.iterdir()) if sub.is_dir() and paths.campaign_toml(sub).exists()
    ]
    if dbg:
        _log.debug("audio_inventory: %d campaign dirs, candidate scan %.2fs", len(campaign_dirs), time.perf_counter() - t0)

    with ThreadPoolExecutor(max_workers=_MAX_STAT_WORKERS) as pool:
        t1 = time.perf_counter() if dbg else 0.0
        structures = _walk_campaigns(campaign_dirs, pool)
        if dbg:
            _log.debug("audio_inventory: structure walk %.2fs", time.perf_counter() - t1)

        all_files = [
            path
            for cards in structures.values()
            for card in cards
            for files in card.by_week.values()
            for path in files
        ]
        t2 = time.perf_counter() if dbg else 0.0
        sizes = _resolve_sizes(all_files, pool)
        if dbg:
            _log.debug("audio_inventory: stat %d files %.2fs", len(all_files), time.perf_counter() - t2)
            _log.debug("audio_inventory: total %.2fs", time.perf_counter() - t0)

    campaigns = tuple(_build_campaign_inventory(d, structures[d], sizes) for d in campaign_dirs)
    return AudioInventory(campaigns=campaigns)


def _walk_campaigns(campaign_dirs: list[Path], pool: ThreadPoolExecutor) -> dict[Path, list[_CardStructure]]:
    """Discover cards/weeks/files under each campaign, batching type checks per level.

    Each level is processed in two steps: list every directory at that level
    (one round trip per directory), then classify every entry found across
    the whole level in a single pool.map() call, rather than classifying
    one entry at a time while descending. This turns what would be
    thousands of sequential is_dir()/is_file() round trips into a handful
    of batches, each parallelized across the pool.
    """
    card_candidates_by_campaign = dict(
        zip(campaign_dirs, pool.map(_list_dir, campaign_dirs), strict=True)
    )
    all_card_candidates = [e for entries in card_candidates_by_campaign.values() for e in entries]
    card_is_dir = _classify(pool, os.DirEntry.is_dir, all_card_candidates)

    card_dirs_by_campaign = {
        campaign_dir: sorted((e for e in entries if card_is_dir[e]), key=lambda e: e.name)
        for campaign_dir, entries in card_candidates_by_campaign.items()
    }

    all_cards = [card for cards in card_dirs_by_campaign.values() for card in cards]
    card_children = dict(
        zip(all_cards, pool.map(lambda e: _list_dir(e.path), all_cards), strict=True)
    )
    all_card_children = [child for children in card_children.values() for child in children]
    child_is_dir = _classify(pool, os.DirEntry.is_dir, all_card_children)

    week_dirs_by_card: dict[os.DirEntry, list[os.DirEntry]] = {}
    loose_by_card: dict[os.DirEntry, list[os.DirEntry]] = {}
    for card_entry in all_cards:
        children = card_children[card_entry]
        week_dirs_by_card[card_entry] = sorted(
            (c for c in children if child_is_dir[c] and _WEEK_DIRNAME.match(c.name)),
            key=lambda e: e.name,
        )
        loose_by_card[card_entry] = [c for c in children if not child_is_dir[c]]

    all_week_dirs = [week for weeks in week_dirs_by_card.values() for week in weeks]
    week_children = dict(
        zip(all_week_dirs, pool.map(lambda w: _list_dir(w.path), all_week_dirs), strict=True)
    )

    all_file_candidates = [f for children in week_children.values() for f in children]
    all_file_candidates += [f for loose in loose_by_card.values() for f in loose]
    is_audio = _classify(pool, _is_audio_entry, all_file_candidates)

    structures: dict[Path, list[_CardStructure]] = {campaign_dir: [] for campaign_dir in campaign_dirs}
    for campaign_dir, card_entries in card_dirs_by_campaign.items():
        for card_entry in card_entries:
            by_week: dict[int, list[Path]] = {}
            for week_entry in week_dirs_by_card[card_entry]:
                week_num = int(_WEEK_DIRNAME.match(week_entry.name).group(1))
                files = sorted((f for f in week_children[week_entry] if is_audio[f]), key=lambda e: e.name)
                by_week[week_num] = [Path(f.path) for f in files]
            loose_files = sorted((f for f in loose_by_card[card_entry] if is_audio[f]), key=lambda e: e.name)
            if loose_files:
                by_week[-1] = [Path(f.path) for f in loose_files]
            structures[campaign_dir].append(
                _CardStructure(name=card_entry.name, folder=Path(card_entry.path), by_week=by_week)
            )
    return structures


def _list_dir(path) -> list[os.DirEntry]:
    with os.scandir(path) as it:
        return list(it)


def _classify[T](pool: ThreadPoolExecutor, fn: Callable[[T], bool], entries: Iterable[T]) -> dict:
    entries = list(entries)
    if not entries:
        return {}
    return dict(zip(entries, pool.map(fn, entries), strict=True))


def _resolve_sizes(file_paths: list[Path], pool: ThreadPoolExecutor) -> dict[Path, int]:
    if not file_paths:
        return {}
    return dict(zip(file_paths, pool.map(_path_size, file_paths), strict=True))


def _path_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _build_campaign_inventory(
    campaign_dir: Path, cards: list[_CardStructure], sizes: dict[Path, int]
) -> CampaignInventory:
    built = [_build_card_inventory(card, sizes) for card in cards]
    built = [card for card in built if card.file_count > 0]
    return CampaignInventory(
        name=campaign_dir.name,
        folder=campaign_dir,
        cards=tuple(built),
        file_count=sum(c.file_count for c in built),
        total_bytes=sum(c.total_bytes for c in built),
    )


def _build_card_inventory(card: _CardStructure, sizes: dict[Path, int]) -> CardInventory:
    weeks: list[WeekInventory] = []
    for week_num in sorted(card.by_week):
        files = tuple(card.by_week[week_num])
        file_sizes = tuple(sizes[p] for p in files)
        weeks.append(WeekInventory(week=week_num, files=files, file_sizes=file_sizes, total_bytes=sum(file_sizes)))
    return CardInventory(
        name=card.name,
        folder=card.folder,
        weeks=tuple(weeks),
        file_count=sum(len(w.files) for w in weeks),
        total_bytes=sum(w.total_bytes for w in weeks),
    )


def _is_audio_entry(entry: os.DirEntry) -> bool:
    return entry.is_file() and os.path.splitext(entry.name)[1].lower() in _AUDIO_SUFFIXES
