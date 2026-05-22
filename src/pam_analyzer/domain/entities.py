from dataclasses import dataclass, field
from pathlib import Path

from .enums import FilterMode, VerifiedState
from .values import LatLon


@dataclass(frozen=True, slots=True)
class Project:
    """Project settings persisted in a .pamproj TOML file.

    Field names mirror the original PAM Analyzer schema for drop-in compatibility.
    """

    path: Path
    audio_recordings_path: Path
    sdcard_name_pattern: str = "^MSD-"
    detections_output_path: Path | None = None
    birdnet_min_conf: float = 0.25
    birdnet_overlap: float = 0.0
    birdnet_locales: tuple[str, ...] = ()
    preferred_species_lang: str = "en"
    snippet_padding_before: float = 0.0
    snippet_padding_after: float = 0.0

    @property
    def name(self) -> str:
        return self.path.stem

    @property
    def output_base(self) -> Path:
        if self.detections_output_path:
            return self.detections_output_path
        return self.audio_recordings_path / f"{self.name}-detections"


@dataclass(frozen=True, slots=True)
class Campaign:
    """A time-bounded ARU deployment. Lives as a folder under audio_recordings_path."""

    name: str
    folder: Path
    species_filter_mode: FilterMode = FilterMode.LOCATION
    location: LatLon | None = None  # required when mode == LOCATION


@dataclass(slots=True)
class Detection:
    """A single BirdNET detection row.

    Mutable: annotation fields (Verified/Corrected_Species/Comment) are user-editable.
    `extra` carries any additional CSV columns not modeled explicitly so that
    drop-in CSV round-tripping preserves data we don't yet understand.
    """

    campaign: str
    aru: str
    week: float | None
    species: str
    scientific_name: str
    confidence: float
    start_time: float
    end_time: float
    rank: float | None
    file: str
    recording_time: str = ""
    lat: float | None = None
    lon: float | None = None
    species_list: str = ""
    min_conf: float | None = None
    model: str = ""
    verified: VerifiedState = VerifiedState.UNSET
    corrected_species: str = ""
    comment: str = ""
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CampaignRunResult:
    campaign_name: str
    output_dir: Path
    detections_csv: Path
    species_list_txt: Path | None  # location mode only
    detection_count: int
    wav_count: int
    aru_count: int
    elapsed: float
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AnalysisRunResult:
    campaigns: tuple[CampaignRunResult, ...]
    elapsed: float = 0.0
    # True when synthesized from on-disk artifacts at project load, rather
    # than from a fresh BirdNET run. The UI uses this to show a different
    # headline ("Loaded previous results" vs "Run finished").
    from_disk: bool = False


@dataclass(frozen=True, slots=True)
class WeekInventory:
    week: int  # BirdNET-style week number; -1 for files outside any week_NN folder
    files: tuple[Path, ...]
    total_bytes: int
    file_sizes: tuple[int, ...] = ()  # parallel to files; populated by audio_inventory_discovery


@dataclass(frozen=True, slots=True)
class CardInventory:
    name: str  # the card folder name as it appears on disk
    folder: Path
    weeks: tuple[WeekInventory, ...]
    file_count: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class CampaignInventory:
    name: str
    folder: Path
    cards: tuple[CardInventory, ...]
    file_count: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class AudioInventory:
    """What audio is on disk under a project's audio_recordings_path.

    The empty inventory (campaigns=()) is the natural 'no project loaded' value
    and also the state before discovery runs.
    """

    campaigns: tuple[CampaignInventory, ...] = ()

    def for_campaign(self, name: str) -> CampaignInventory | None:
        for c in self.campaigns:
            if c.name == name:
                return c
        return None
