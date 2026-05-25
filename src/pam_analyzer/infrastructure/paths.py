"""Conventions for where things live on disk. Centralized so paths aren't hardcoded across repos."""

from pathlib import Path

AUDIO_EXTENSIONS: frozenset[str] = frozenset({
    ".wav", ".flac", ".mp3", ".ogg", ".m4a", ".wma", ".aiff", ".aif"
})


def campaign_toml(campaign_folder: Path) -> Path:
    return campaign_folder / "campaign.toml"


def species_list_file(campaign_folder: Path) -> Path:
    return campaign_folder / "species_list.txt"


def must_have_species_file(campaign_folder: Path) -> Path:
    return campaign_folder / "must_have_species.txt"


def campaign_csv(output_base: Path, campaign_name: str) -> Path:
    """Legacy single-model CSV path (no model_key suffix).

    Returned even when the file doesn't exist. Loader code treats this
    path as the historical fallback when scanning for a campaign's data;
    new runs write the suffixed variant via campaign_csv_for_model.
    """
    return output_base / campaign_name / f"{campaign_name}-detections.csv"


def campaign_csv_for_model(output_base: Path, campaign_name: str, model_key: str) -> Path:
    """CSV path for a specific model run within a campaign.

    Different model runs (BirdNET, Perch v2, ...) write into the same
    campaign directory under different filenames so the panel can load
    them all and aggregate via the Model column.
    """
    return output_base / campaign_name / f"{campaign_name}-detections-{model_key}.csv"


def campaign_csvs(output_base: Path, campaign_name: str) -> list[Path]:
    """All detection CSVs for a campaign, oldest layout first.

    Returns the legacy unsuffixed file (if present) followed by every
    per-model suffixed file found in the campaign directory, sorted by
    name. Used by the repo and the discovery code to enumerate every
    run a user has accumulated for one campaign without hardcoding
    model keys.
    """
    camp_dir = output_base / campaign_name
    if not camp_dir.is_dir():
        return []
    found: list[Path] = []
    legacy = campaign_csv(output_base, campaign_name)
    if legacy.exists():
        found.append(legacy)
    prefix = f"{campaign_name}-detections-"
    found.extend(sorted(p for p in camp_dir.glob(f"{prefix}*.csv") if p.is_file()))
    return found
