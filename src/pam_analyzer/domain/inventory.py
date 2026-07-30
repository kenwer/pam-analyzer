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


@dataclass(frozen=True, slots=True)
class AnalysisInventoryEntry:
    """One detection CSV found on disk, described for display.

    A found artifact, not the result of a run: it carries only what discovery
    can read back from disk (where the CSV and the applied species list are, how
    many detections the CSV holds, which model produced it). It has no temporal
    or input-scope data, so it never carries the sentinel elapsed=0.0 or
    warnings=() a reused run type would leave behind. Multiple models of the
    same campaign coexist as sibling entries tagged by model_key.
    """

    campaign_name: str
    output_dir: Path
    detections_csv: Path
    species_list_txt: Path | None  # location mode only
    detection_count: int
    model_key: str = ""  # model that produced this CSV, inferred from its filename


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
    model, one AnalysisInventoryEntry per CSV.
    """

    campaigns: tuple[AnalysisInventoryEntry, ...]
