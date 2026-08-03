"""The Species filter: a campaign's rule for which species a run may emit.

Two modes (FilterMode): LOCATION derives the allow-list from coordinates
(optionally merged with a must-have list), LIST supplies an explicit species
list. A SpeciesFilter is a value object that loads and saves its own sidecar
files and resolves itself into a ResolvedSpeciesFilter, the per-week allow-list
the analysis runner filters detections against.

Resolution needs a region_species lookup: given lat, lon, and a birdnet week,
which species occur there. That lookup reaches the birdnet lib, so it is passed
in as a plain callable rather than imported here, keeping this module Qt-free
and infrastructure-free.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .audio_import import WEEK_YEAR_ROUND, week_from_path
from .enums import FilterMode
from .values import LatLon

RegionSpecies = Callable[[float, float, int], frozenset[str]]
"""Given (latitude, longitude, birdnet week), the scientific names that occur there."""

ExpandSynonyms = Callable[[frozenset[str]], frozenset[str]]
"""Expand a set of scientific names to include their cross-taxonomy equivalents."""


def _identity_synonyms(names: frozenset[str]) -> frozenset[str]:
    return names


def parse_species_lines(text: str) -> frozenset[str]:
    """Parse a user-supplied species blob into a set of scientific names.

    Accepts plain Latin names or 'Scientific_Common' entries copied from a
    BirdNET-style species list, so users can paste either format without
    converting. Everything after a '#' on a line is treated as a comment, so
    users can annotate their lists or paste back lines a runner emitted with
    '  # must-have' markers.
    """
    out: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        out.add(line.split("_", 1)[0].strip())
    return frozenset(out)


@dataclass(frozen=True, slots=True)
class ResolvedSpeciesFilter:
    """The applied allow-list a SpeciesFilter produces for one run.

    allowed_for(path) returns the set a detection's species must belong to, or
    None to keep every row (no filtering). LIST mode uses one fixed set for
    every file. LOCATION mode looks the file's week up in per_week_allowed.
    per_week_allowed and must_haves feed the applied-species-list sidecar the
    runner writes, so a user can see exactly what was filtered against.
    """

    location: LatLon | None
    fixed_allowed: frozenset[str] | None
    per_week_allowed: dict[int, frozenset[str]]
    must_haves: frozenset[str]

    def allowed_for(self, path: Path) -> frozenset[str] | None:
        if self.fixed_allowed is not None:
            return self.fixed_allowed
        if not self.per_week_allowed:
            return None
        week = week_from_path(path)
        return self.per_week_allowed.get(week if week is not None else WEEK_YEAR_ROUND)


@dataclass(frozen=True, slots=True)
class SpeciesFilter:
    """A campaign's rule for which species a run may emit.

    Holds the mode and its inputs: a location for LOCATION mode, the list text
    for LIST mode, and the optional must-have text merged on top in LOCATION
    mode. Reads and writes its own sidecar files under the campaign folder.
    """

    mode: FilterMode
    location: LatLon | None = None
    list_text: str = ""
    must_have_text: str = ""

    @classmethod
    def load(cls, folder: Path, mode: FilterMode, location: LatLon | None) -> SpeciesFilter:
        """Load the filter for a campaign, reading only the sidecar its mode uses."""
        list_text = ""
        must_have_text = ""
        if mode == FilterMode.LIST:
            list_text = _read(paths.species_list_file(folder))
        elif mode == FilterMode.LOCATION:
            must_have_text = _read(paths.must_have_species_file(folder))
        return cls(mode=mode, location=location, list_text=list_text, must_have_text=must_have_text)

    def save(self, folder: Path) -> None:
        """Persist the filter to the sidecar file its mode uses."""
        if self.mode == FilterMode.LIST:
            paths.species_list_file(folder).write_text(self.list_text, encoding="utf-8")
        elif self.mode == FilterMode.LOCATION:
            paths.must_have_species_file(folder).write_text(self.must_have_text, encoding="utf-8")

    def resolve(
        self,
        wav_files: list[Path],
        region_species: RegionSpecies,
        expand_synonyms: ExpandSynonyms = _identity_synonyms,
    ) -> ResolvedSpeciesFilter:
        """Compute the allow-list this filter applies to a run's audio files.

        LIST mode yields one fixed set for every file. LOCATION mode computes a
        per-week set (the region's species for each week present, merged with
        the must-have list) so a run against week_NN folders filters each week
        against its own seasonal list. Any other case (LIST with no text,
        LOCATION with no location) yields an empty filter that keeps every row.

        The per-week sets are computed eagerly here so a geo download happens
        during the runner's 'preparing' phase, not mid-inference.

        expand_synonyms bridges the two model taxonomies for the user-authored
        names only (the LIST text and the must-have text): each name is expanded
        to include its cross-taxonomy equivalent, so a bird typed in either
        spelling matches whichever model runs. The region_species output is left
        as-is, so LOCATION mode's regional list stays on BirdNET's axis.
        """
        if self.mode == FilterMode.LIST and self.list_text:
            fixed = expand_synonyms(parse_species_lines(self.list_text))
            return ResolvedSpeciesFilter(None, fixed, {}, frozenset())
        if self.mode == FilterMode.LOCATION and self.location is not None:
            lat, lon = self.location.latitude, self.location.longitude
            must_haves = expand_synonyms(parse_species_lines(self.must_have_text))
            weeks_present: set[int] = set()
            for f in wav_files:
                week = week_from_path(f)
                weeks_present.add(week if week is not None else WEEK_YEAR_ROUND)
            per_week = {w: region_species(lat, lon, w) | must_haves for w in weeks_present}
            return ResolvedSpeciesFilter(self.location, None, per_week, must_haves)
        return ResolvedSpeciesFilter(None, None, {}, frozenset())


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""
