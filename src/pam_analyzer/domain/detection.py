from dataclasses import dataclass, field

from .enums import VerifiedState


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
