"""Conventions for where things live on disk. Centralized so paths aren't hardcoded across repos."""

from pathlib import Path

from platformdirs import user_log_dir

from ..domain import detection_schema

AUDIO_EXTENSIONS: frozenset[str] = frozenset({
    ".wav", ".flac", ".mp3", ".ogg", ".m4a", ".wma", ".aiff", ".aif"
})


def log_dir() -> Path:
    return Path(user_log_dir("PAM Analyzer", appauthor=False))


def campaign_toml(campaign_folder: Path) -> Path:
    return campaign_folder / "campaign.toml"


def species_list_file(campaign_folder: Path) -> Path:
    return campaign_folder / "species_list.txt"


def must_have_species_file(campaign_folder: Path) -> Path:
    return campaign_folder / "must_have_species.txt"


def campaign_csv_for_model(output_base: Path, campaign_name: str, model_key: str) -> Path:
    """CSV path for a specific model run within a campaign.

    Different model runs (BirdNET, Perch v2, ...) write into the same
    campaign directory under different filenames so the panel can load
    them all and aggregate via the Model column.
    """
    return output_base / campaign_name / detection_schema.detections_csv_name(campaign_name, model_key)


def campaign_csvs(output_base: Path, campaign_name: str) -> list[Path]:
    """All detection CSVs for a campaign, sorted by name."""
    camp_dir = output_base / campaign_name
    if not camp_dir.is_dir():
        return []
    return sorted(
        p
        for p in camp_dir.iterdir()
        if p.is_file() and detection_schema.model_key_from_csv_name(campaign_name, p.name) is not None
    )
