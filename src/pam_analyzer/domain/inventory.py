"""The on-disk inventories: what a project contains, discovered not persisted.

Two frozen value-object trees:
  * AudioInventory holds campaigns, each CampaignInventory holds cards, each
    CardInventory holds weeks (built by infrastructure.audio_inventory_discovery).
  * AnalysisInventory holds the detection results found across all campaigns and
    models (built by infrastructure.analysis_inventory_discovery).
Both are the audio and analysis sides of the same idea: a catalog of what exists
on disk, rebuilt on demand.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .analysis_run_result import CampaignResult


@dataclass(frozen=True, slots=True)
class WeekInventory:
    week: int  # BirdNET week number; audio_import.WEEK_YEAR_ROUND for files outside week_NN folders
    files: tuple[Path, ...]
    total_bytes: int
    # Earliest/latest recording time parsed from filenames; None when none parse.
    date_range: tuple[datetime, datetime] | None
    file_sizes: tuple[int, ...] = ()  # parallel to files; populated by audio_inventory_discovery


@dataclass(frozen=True, slots=True)
class CardInventory:
    name: str  # the card folder name as it appears on disk
    folder: Path
    weeks: tuple[WeekInventory, ...]
    file_count: int
    total_bytes: int
    date_range: tuple[datetime, datetime] | None  # merged from this card's weeks


@dataclass(frozen=True, slots=True)
class CampaignInventory:
    name: str
    folder: Path
    cards: tuple[CardInventory, ...]
    file_count: int
    total_bytes: int
    date_range: tuple[datetime, datetime] | None  # merged from this campaign's cards


@dataclass(frozen=True, slots=True)
class AudioInventory:
    """What audio is on disk under a project folder.

    The empty inventory (campaigns=()) is the natural 'no project loaded' value
    and also the state before discovery runs.
    """

    campaigns: tuple[CampaignInventory, ...] = ()

    def for_campaign(self, name: str) -> CampaignInventory | None:
        for c in self.campaigns:
            if c.name == name:
                return c
        return None


@dataclass(frozen=True, slots=True)
class AnalysisInventory:
    """Every detection result on disk under a project, as a display view.

    Built by discovery from all campaign CSVs, so it spans every campaign and
    model, one CampaignResult per CSV.
    """

    campaigns: tuple[CampaignResult, ...]
