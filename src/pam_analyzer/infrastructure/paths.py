"""Conventions for where things live on disk. Centralized so paths aren't hardcoded across repos."""

from pathlib import Path

AUDIO_EXTENSIONS: frozenset[str] = frozenset({
    ".wav", ".flac", ".mp3", ".ogg", ".m4a", ".wma", ".aiff", ".aif"
})


def campaign_toml(campaign_folder: Path) -> Path:
    return campaign_folder / "campaign.toml"


def species_list_file(campaign_folder: Path) -> Path:
    return campaign_folder / "species_list.txt"


def campaign_csv(output_base: Path, campaign_name: str) -> Path:
    return output_base / campaign_name / f"{campaign_name}-detections.csv"


def combined_csv(output_base: Path, project_name: str) -> Path:
    return output_base / f"{project_name}-detections.csv"
