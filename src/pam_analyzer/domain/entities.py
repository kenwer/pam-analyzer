import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .enums import FilterMode, VerifiedState
from .values import LatLon


@dataclass(frozen=True, slots=True)
class Project:
    """Project settings persisted as pam-analyzer.toml inside the project folder.

    The folder is the project: it holds the settings file and one subfolder
    per campaign, so a project stores no paths and can be moved freely.
    """

    folder: Path
    sdcard_name_pattern: str = "^(MSD-|2MM)"  # AudioMoth (MSD-) and Song Meter (2MM serials)
    analysis_model: str = "BirdNET-2.4"
    birdnet_min_conf: float = 0.25
    birdnet_overlap: float = 0.0
    birdnet_locales: tuple[str, ...] = ()
    preferred_species_lang: str = "en"
    snippet_padding_before: float = 0.0
    snippet_padding_after: float = 0.0

    @property
    def name(self) -> str:
        return self.folder.name


@dataclass(frozen=True, slots=True)
class Campaign:
    """A time-bounded ARU deployment. Lives as a folder under the project folder."""

    name: str
    folder: Path
    species_filter_mode: FilterMode = FilterMode.LOCATION
    location: LatLon | None = None  # required when mode == LOCATION


# Names Windows refuses or silently alters. Enforced on every platform so a
# project folder stays portable between macOS and Windows machines.
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def campaign_name_error(name: str, taken_names: Iterable[str] = ()) -> str | None:
    """Why the (already stripped) name cannot be used as a campaign folder
    name, or None if it can. The message is suitable for showing to the user.

    Duplicates are compared NFC-normalized because some filesystems (HFS+,
    certain network mounts) store names in NFD form, which the OS treats as
    the same folder even though the strings compare unequal.
    """
    if not name:
        return "Campaign name must not be empty."
    if "/" in name or "\\" in name:
        return "Campaign name must not contain slashes."
    # Win32 strips trailing dots when creating a folder, so the name on disk
    # would differ from the typed one.
    if name.endswith("."):
        return "Campaign name must not end with a dot."
    if name.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES:
        return f'"{name}" is a reserved name on Windows.'
    taken = {unicodedata.normalize("NFC", n) for n in taken_names}
    if unicodedata.normalize("NFC", name) in taken:
        return f'A campaign named "{name}" already exists.'
    return None


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
    # CSV path this detection was loaded from. Not persisted (the field
    # is omitted from CSV writes). Used by CsvDetectionRepository.save to
    # route edits back to the file they came from when multiple model
    # runs share a campaign directory.
    source_path: Path | None = None
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
    model_key: str = "" # Model that produced this row (allows the BirdNET panel to add hints)


@dataclass(frozen=True, slots=True)
class AnalysisRunResult:
    campaigns: tuple[CampaignRunResult, ...]
    elapsed: float = 0.0


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
