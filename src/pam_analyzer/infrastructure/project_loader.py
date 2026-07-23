"""Composes the filesystem reads needed to open a project folder.

Kept Qt-free and separate from AppState so the same sequence can run either
synchronously (tests, scripts) or on a background thread (ProjectLoadWorker)
without duplicating the read order in two places.
"""

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from ..domain import AnalysisRunResult, AudioInventory, Campaign, Project
from .analysis_discovery import discover_analysis_result
from .audio_inventory_discovery import discover_audio_inventory
from .toml_campaign_repo import TomlCampaignRepository
from .toml_project_repo import TomlProjectRepository

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectLoadResult:
    project: Project
    campaigns: list[Campaign]
    audio_inventory: AudioInventory
    analysis_result: AnalysisRunResult | None


def load_project_bundle(
    project_repo: TomlProjectRepository,
    campaign_repo: TomlCampaignRepository,
    folder: Path,
) -> ProjectLoadResult:
    """Read a project folder and everything derived from it.

    Each step is a separate filesystem pass over `folder`, which is what
    makes this slow enough on a network-mounted (e.g. CIFS) folder to be
    worth running off the UI thread. Per-step timing is logged at DEBUG to
    make that kind of slowdown diagnosable after the fact.
    """
    dbg = _log.isEnabledFor(logging.DEBUG)
    t0 = time.perf_counter() if dbg else 0.0

    t = t0
    project = project_repo.load(folder)
    if dbg:
        t, prev = time.perf_counter(), t
        _log.debug("load_project_bundle: repo.load %.2fs", t - prev)

    campaigns = campaign_repo.discover(project.folder)
    if dbg:
        t, prev = time.perf_counter(), t
        _log.debug("load_project_bundle: discover campaigns %.2fs", t - prev)

    audio_inventory = discover_audio_inventory(project.folder)
    if dbg:
        t, prev = time.perf_counter(), t
        _log.debug("load_project_bundle: discover_audio_inventory %.2fs", t - prev)

    analysis_result = discover_analysis_result(project.folder)
    if dbg:
        t, prev = time.perf_counter(), t
        _log.debug("load_project_bundle: discover_analysis_result %.2fs", t - prev)
        _log.debug("load_project_bundle: total %.2fs", t - t0)

    return ProjectLoadResult(
        project=project,
        campaigns=campaigns,
        audio_inventory=audio_inventory,
        analysis_result=analysis_result,
    )
