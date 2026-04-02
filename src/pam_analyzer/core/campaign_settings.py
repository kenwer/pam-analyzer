import tomllib
from dataclasses import dataclass
from pathlib import Path

CAMPAIGN_TOML = 'campaign.toml'
SPECIES_LIST_FILENAME = 'species_list.txt'


@dataclass
class CampaignSettings:
    """Per-campaign configuration stored in campaign.toml inside the campaign folder.

    A campaign is a time-bounded deployment of one or more ARUs. Its settings travel
    with the audio folder independently of the .pamproj file, making campaigns
    self-contained (moveable, archiveable, shareable).

    BirdNET requires either geographic coordinates (for species range filtering) or a
    fixed species list. These two modes are mutually exclusive and are set at campaign
    creation time.

    Fields:
        species_filter_mode: 'location' - filter by lat/lon and week; or
                             'list'     - filter by a fixed species list file.
        latitude:            Decimal degrees, -90 to 90. Only used in 'location' mode.
        longitude:           Decimal degrees, -180 to 180. Only used in 'location' mode.

    When mode is 'list', the species list is always stored as species_list.txt inside
    the campaign folder, making campaigns fully self-contained without storing a path.
    """

    species_filter_mode: str = 'location'  # 'location' | 'list'
    latitude: float = 0.0
    longitude: float = 0.0

    @classmethod
    def load(cls, campaign_dir: Path) -> 'CampaignSettings':
        """Load settings from campaign.toml in campaign_dir."""
        with open(campaign_dir / CAMPAIGN_TOML, 'rb') as f:
            data = tomllib.load(f)
        return cls(
            species_filter_mode=data.get('species_filter_mode', 'location'),
            latitude=data.get('latitude', 0.0),
            longitude=data.get('longitude', 0.0),
        )

    def save(self, campaign_dir: Path) -> None:
        """Write settings to campaign.toml in campaign_dir.

        Only fields relevant to the active species_filter_mode are written,
        keeping the file minimal and human-readable.
        """
        lines = [f'species_filter_mode = "{self.species_filter_mode}"\n']
        if self.species_filter_mode == 'location':
            lines.append(f'latitude = {self.latitude}\n')
            lines.append(f'longitude = {self.longitude}\n')
        (campaign_dir / CAMPAIGN_TOML).write_text(''.join(lines))


def discover_campaigns(audio_root: Path) -> dict[str, Path]:
    """Return {name: path} of campaigns found under audio_root, sorted most-recent first.

    A directory is considered a campaign if it contains a campaign.toml file.
    Returns an empty dict if audio_root does not exist.
    """
    if not audio_root.exists():
        return {}
    dirs = [d for d in audio_root.iterdir() if d.is_dir() and (d / CAMPAIGN_TOML).exists()]
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return {d.name: d for d in dirs}
