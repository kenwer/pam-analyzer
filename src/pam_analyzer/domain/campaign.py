import logging
import shutil
import time
import tomllib
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

import tomli_w

from . import paths
from .enums import FilterMode
from .values import LatLon

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Campaign:
    """A time-bounded ARU deployment. Lives as a folder under the project folder.

    A campaign persists itself: its settings live in campaign.toml and its
    species filter in a sidecar text file, both inside its folder.
    """

    name: str
    folder: Path
    species_filter_mode: FilterMode = FilterMode.LOCATION
    location: LatLon | None = None  # required when mode == LOCATION

    @classmethod
    def load(cls, name: str, folder: Path) -> "Campaign":
        with open(paths.campaign_toml(folder), "rb") as f:
            data = tomllib.load(f)
        mode = FilterMode(data.get("species_filter_mode", FilterMode.LOCATION.value))
        location: LatLon | None = None
        if mode == FilterMode.LOCATION:
            location = LatLon(
                latitude=float(data.get("latitude", 0.0)),
                longitude=float(data.get("longitude", 0.0)),
            )
        return cls(name=name, folder=folder, species_filter_mode=mode, location=location)

    @classmethod
    def discover(cls, project_folder: Path) -> list["Campaign"]:
        """Every campaign under a project folder, newest folder first."""
        dbg = _log.isEnabledFor(logging.DEBUG)

        t0 = time.perf_counter() if dbg else 0.0
        candidates = paths.campaign_folders(project_folder)
        if dbg:
            _log.debug("Campaign.discover: %d candidates, scan %.2fs", len(candidates), time.perf_counter() - t0)

        t1 = time.perf_counter() if dbg else 0.0
        candidates.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        if dbg:
            _log.debug("Campaign.discover: sort (stat x%d) %.2fs", len(candidates), time.perf_counter() - t1)

        t2 = time.perf_counter() if dbg else 0.0
        result = [cls.load(d.name, d) for d in candidates]
        if dbg:
            _log.debug("Campaign.discover: load %d campaigns %.2fs", len(result), time.perf_counter() - t2)
        return result

    def save(self) -> None:
        data: dict = {"species_filter_mode": self.species_filter_mode.value}
        if self.species_filter_mode == FilterMode.LOCATION and self.location is not None:
            data["latitude"] = self.location.latitude
            data["longitude"] = self.location.longitude
        self.folder.mkdir(parents=True, exist_ok=True)
        with open(paths.campaign_toml(self.folder), "wb") as f:
            tomli_w.dump(data, f)

    def create(self) -> None:
        if self.folder.exists():
            raise FileExistsError(self.folder)
        self.save()

    def delete(self) -> None:
        if self.folder.exists():
            shutil.rmtree(self.folder)

    def rename(self, new_name: str) -> "Campaign":
        """Rename this campaign's folder and return the moved campaign.

        Returns a new instance because Campaign is frozen; the old one still
        points at the now-nonexistent folder and should be discarded.
        """
        new_folder = self.folder.parent / new_name
        self.folder.rename(new_folder)
        return replace(self, name=new_name, folder=new_folder)

    def read_species_list(self) -> str:
        f = paths.species_list_file(self.folder)
        return f.read_text(encoding="utf-8") if f.exists() else ""

    def write_species_list(self, content: str) -> None:
        paths.species_list_file(self.folder).write_text(content, encoding="utf-8")

    def read_must_have_species(self) -> str:
        f = paths.must_have_species_file(self.folder)
        return f.read_text(encoding="utf-8") if f.exists() else ""

    def has_must_have_species(self) -> bool:
        """Whether a non-empty must-have list exists, via a stat (no file read)."""
        f = paths.must_have_species_file(self.folder)
        return f.exists() and f.stat().st_size > 0

    def write_must_have_species(self, content: str) -> None:
        paths.must_have_species_file(self.folder).write_text(content, encoding="utf-8")

    def write_species_filter(self, species_text: str, must_have_text: str) -> None:
        """Persist the species filter to the sidecar file for this campaign's mode."""
        if self.species_filter_mode == FilterMode.LIST:
            self.write_species_list(species_text)
        elif self.species_filter_mode == FilterMode.LOCATION:
            self.write_must_have_species(must_have_text)

    def count_audio_files(self) -> int:
        if not self.folder.exists():
            return 0
        return sum(
            1
            for p in self.folder.rglob("*")
            if p.is_file() and p.suffix.lower() in paths.AUDIO_EXTENSIONS
        )

    @staticmethod
    def name_error(name: str, taken_names: Iterable[str] = ()) -> str | None:
        """Why the (already stripped) name cannot be used as a campaign folder
        name, or None if it can. See the module-level campaign_name_error."""
        return campaign_name_error(name, taken_names)


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
