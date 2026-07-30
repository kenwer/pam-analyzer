"""The on-disk inventories: what a project contains, discovered not persisted.

Two frozen value-object trees:
  * AudioInventory holds campaigns, each CampaignInventory holds cards, each
    CardInventory holds weeks (built by infrastructure.audio_inventory_discovery).
  * AnalysisInventory holds the detection results found across all campaigns and
    models (built by infrastructure.analysis_inventory_discovery).
Both are the audio and analysis sides of the same idea: a catalog of what exists
on disk, rebuilt on demand.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
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
    # Byte totals are resolved in a second pass that stats each file, which is
    # the slow part on a network mount. None means exactly "sizes pending": a
    # scope with no files is 0, known without a stat. Read size_pending, which
    # every display site uses instead of testing None, so the meaning stays here.
    total_bytes: int | None
    # Earliest/latest recording time parsed from filenames; None when none parse.
    date_range: tuple[datetime, datetime] | None
    file_sizes: tuple[int, ...] | None = None  # parallel to files, None until sizes are resolved

    @property
    def size_pending(self) -> bool:
        """True while this scope's byte total is still awaiting the size pass."""
        return self.total_bytes is None

    def with_sizes(self, sizes: Mapping[Path, int]) -> "WeekInventory":
        """Return a copy with per-file sizes and the byte total filled in.

        A pure transform: sizes is a path-to-bytes map the caller has already
        stat'd. Files missing from the map count as 0, matching how the on-disk
        stat treats an unreadable file.
        """
        file_sizes = tuple(sizes.get(path, 0) for path in self.files)
        return replace(self, file_sizes=file_sizes, total_bytes=sum(file_sizes))


@dataclass(frozen=True, slots=True)
class CardInventory:
    name: str  # the card folder name as it appears on disk
    folder: Path
    weeks: tuple[WeekInventory, ...]
    file_count: int
    total_bytes: int | None  # None until sizes are resolved (see WeekInventory)
    date_range: tuple[datetime, datetime] | None  # merged from this card's weeks

    @property
    def size_pending(self) -> bool:
        """True while this scope's byte total is still awaiting the size pass."""
        return self.total_bytes is None

    def with_sizes(self, sizes: Mapping[Path, int]) -> "CardInventory":
        weeks = tuple(week.with_sizes(sizes) for week in self.weeks)
        return replace(self, weeks=weeks, total_bytes=sum(week.total_bytes for week in weeks))


@dataclass(frozen=True, slots=True)
class CampaignInventory:
    name: str
    folder: Path
    cards: tuple[CardInventory, ...]
    file_count: int
    total_bytes: int | None  # None until sizes are resolved (see WeekInventory)
    date_range: tuple[datetime, datetime] | None  # merged from this campaign's cards

    @property
    def size_pending(self) -> bool:
        """True while this scope's byte total is still awaiting the size pass."""
        return self.total_bytes is None

    def with_sizes(self, sizes: Mapping[Path, int]) -> "CampaignInventory":
        cards = tuple(card.with_sizes(sizes) for card in self.cards)
        return replace(self, cards=cards, total_bytes=sum(card.total_bytes for card in cards))


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

    def iter_files(self) -> Iterator[Path]:
        """Every audio file path across all campaigns, cards, and weeks."""
        for campaign in self.campaigns:
            for card in campaign.cards:
                for week in card.weeks:
                    yield from week.files

    def with_sizes(self, sizes: Mapping[Path, int]) -> "AudioInventory":
        """Return a fully sized copy: the size pass stats iter_files(), then hands
        the resulting path-to-bytes map here to fill in every total_bytes."""
        return AudioInventory(campaigns=tuple(c.with_sizes(sizes) for c in self.campaigns))

    @property
    def sizes_pending(self) -> bool:
        """True when the structure is known but per-file sizes are not yet resolved."""
        return any(c.size_pending for c in self.campaigns)


@dataclass(frozen=True, slots=True)
class AnalysisInventory:
    """Every detection result on disk under a project, as a display view.

    Built by discovery from all campaign CSVs, so it spans every campaign and
    model, one AnalysisInventoryEntry per CSV.
    """

    campaigns: tuple[AnalysisInventoryEntry, ...]
