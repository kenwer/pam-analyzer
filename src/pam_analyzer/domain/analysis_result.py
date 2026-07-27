from dataclasses import dataclass
from pathlib import Path


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
