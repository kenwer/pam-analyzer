"""Reads/writes campaign.toml files and discovers campaigns under an audio root."""

import dataclasses
import logging
import shutil
import time
import tomllib
from pathlib import Path

import tomli_w

from ..domain import Campaign, FilterMode, LatLon
from . import paths

_log = logging.getLogger(__name__)


class TomlCampaignRepository:
    def discover(self, audio_root: Path) -> list[Campaign]:
        if not audio_root.exists():
            return []
        dbg = _log.isEnabledFor(logging.DEBUG)

        t0 = time.perf_counter() if dbg else 0.0
        candidates = [
            d
            for d in audio_root.iterdir()
            if d.is_dir() and paths.campaign_toml(d).exists()
        ]
        if dbg:
            _log.debug("TomlCampaignRepository.discover: %d candidates, scan %.2fs", len(candidates), time.perf_counter() - t0)

        t1 = time.perf_counter() if dbg else 0.0
        candidates.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        if dbg:
            _log.debug("TomlCampaignRepository.discover: sort (stat x%d) %.2fs", len(candidates), time.perf_counter() - t1)

        t2 = time.perf_counter() if dbg else 0.0
        result = [self.load(d.name, d) for d in candidates]
        if dbg:
            _log.debug("TomlCampaignRepository.discover: load %d campaigns %.2fs", len(result), time.perf_counter() - t2)
        return result

    def load(self, name: str, folder: Path) -> Campaign:
        with open(paths.campaign_toml(folder), "rb") as f:
            data = tomllib.load(f)
        mode = FilterMode(data.get("species_filter_mode", FilterMode.LOCATION.value))
        location: LatLon | None = None
        if mode == FilterMode.LOCATION:
            location = LatLon(
                latitude=float(data.get("latitude", 0.0)),
                longitude=float(data.get("longitude", 0.0)),
            )
        return Campaign(
            name=name,
            folder=folder,
            species_filter_mode=mode,
            location=location,
        )

    def save(self, campaign: Campaign) -> None:
        data: dict = {"species_filter_mode": campaign.species_filter_mode.value}
        if campaign.species_filter_mode == FilterMode.LOCATION and campaign.location is not None:
            data["latitude"] = campaign.location.latitude
            data["longitude"] = campaign.location.longitude
        campaign.folder.mkdir(parents=True, exist_ok=True)
        with open(paths.campaign_toml(campaign.folder), "wb") as f:
            tomli_w.dump(data, f)

    def delete(self, campaign: Campaign) -> None:
        if campaign.folder.exists():
            shutil.rmtree(campaign.folder)

    def create(self, campaign: Campaign) -> None:
        if campaign.folder.exists():
            raise FileExistsError(campaign.folder)
        self.save(campaign)

    def rename(self, campaign: Campaign, new_name: str) -> Campaign:
        new_folder = campaign.folder.parent / new_name
        campaign.folder.rename(new_folder)
        return dataclasses.replace(campaign, name=new_name, folder=new_folder)

    def read_species_list(self, campaign: Campaign) -> str:
        f = paths.species_list_file(campaign.folder)
        return f.read_text(encoding="utf-8") if f.exists() else ""

    def write_species_list(self, campaign: Campaign, content: str) -> None:
        paths.species_list_file(campaign.folder).write_text(content, encoding="utf-8")

    def read_must_have_species(self, campaign: Campaign) -> str:
        f = paths.must_have_species_file(campaign.folder)
        return f.read_text(encoding="utf-8") if f.exists() else ""

    def has_must_have_species(self, campaign: Campaign) -> bool:
        """Whether a non-empty must-have list exists, via a stat (no file read)."""
        f = paths.must_have_species_file(campaign.folder)
        return f.exists() and f.stat().st_size > 0

    def write_must_have_species(self, campaign: Campaign, content: str) -> None:
        paths.must_have_species_file(campaign.folder).write_text(content, encoding="utf-8")

    def count_audio_files(self, campaign: Campaign) -> int:
        if not campaign.folder.exists():
            return 0
        return sum(
            1
            for p in campaign.folder.rglob("*")
            if p.is_file() and p.suffix.lower() in paths.AUDIO_EXTENSIONS
        )
