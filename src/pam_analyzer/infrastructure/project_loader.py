"""Composes the filesystem reads needed to open a project folder.

Kept Qt-free and separate from AppState so the same sequence can run either
synchronously (tests, scripts) or on a background thread (ProjectLoadWorker)
without duplicating the read order in two places.
"""

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from ..domain import AnalysisInventory, AudioInventory, Campaign, Project
from .analysis_inventory_discovery import discover_analysis_inventory
from .audio_inventory_discovery import discover_audio_structure

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectLoadResult:
    project: Project
    campaigns: list[Campaign]
    audio_inventory: AudioInventory
    analysis_inventory: AnalysisInventory | None


def load_project(folder: Path) -> ProjectLoadResult:
    """Read a project folder and everything derived from it.

    Each step is a separate filesystem pass over `folder`, which is what
    makes this slow enough on a network-mounted (e.g. CIFS) folder to be
    worth running off the UI thread. Per-step timing is logged at DEBUG to
    make that kind of slowdown diagnosable after the fact.

    The audio pass here is discover_audio_structure, not the fully sized
    discover_audio_inventory: it reads counts, the tree, and date ranges but
    leaves per-file sizes unresolved (total_bytes is None). Statting every
    file is the slowest part on a network mount, so the caller resolves sizes
    afterward on a separate thread (see workers.AudioInventoryRefresher.refresh,
    given this already-walked inventory) and lets the UI show the tree in the
    meantime.
    """
    dbg = _log.isEnabledFor(logging.DEBUG)
    t0 = time.perf_counter() if dbg else 0.0

    t = t0
    project = Project.load(folder)
    if dbg:
        t, prev = time.perf_counter(), t
        _log.debug("load_project: load project %.2fs", t - prev)

    campaigns = Campaign.discover(project.folder)
    if dbg:
        t, prev = time.perf_counter(), t
        _log.debug("load_project: discover campaigns %.2fs", t - prev)

    audio_inventory = discover_audio_structure(project.folder)
    if dbg:
        t, prev = time.perf_counter(), t
        _log.debug("load_project: discover_audio_structure %.2fs", t - prev)

    analysis_inventory = discover_analysis_inventory(project.folder)
    if dbg:
        t, prev = time.perf_counter(), t
        _log.debug("load_project: discover_analysis_inventory %.2fs", t - prev)
        _log.debug("load_project: total %.2fs", t - t0)

    return ProjectLoadResult(
        project=project,
        campaigns=campaigns,
        audio_inventory=audio_inventory,
        analysis_inventory=analysis_inventory,
    )
