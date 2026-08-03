"""BirdNET-2.4 <-> Perch-2.0 scientific-name crosswalk.

The two detectors label their classes under different taxonomies (BirdNET on
eBird 2021, Perch on iNaturalist 2024), so a genus split renames some birds:
BirdNET's Accipiter gentilis is Perch's Astur gentilis. This module loads a
curated table of those renames (data/taxonomy_crosswalk.tsv) and exposes two
operations on it:

- expand_species: for matching. Given a set of user-typed scientific names,
  return the same set plus every cross-axis equivalent, so a name typed in
  either spelling matches whichever model runs.
- to_axis: for output. Rewrite one scientific name into a chosen taxonomy, so
  every detection in a project can be written under one consistent axis.

The table holds only renamed pairs. Names shared verbatim by both axes are
absent from it, so a plain dict.get(name, name) leaves them untouched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

_log = logging.getLogger(__name__)

# Taxonomy identifiers. They match the model_key strings of the runners that
# emit each axis, which is also what the project-settings combo stores.
BIRDNET_2_4 = "BirdNET-2.4"
PERCH_2_0 = "Perch-2.0"

# The taxonomies a project can normalize its output to, in UI-display order.
TAXONOMIES = (BIRDNET_2_4, PERCH_2_0)

_DATA_FILE = "taxonomy_crosswalk.tsv"


@dataclass(frozen=True)
class _CrosswalkMaps:
    """Directional and symmetric views of the rename table."""

    birdnet_to_perch: dict[str, str]
    perch_to_birdnet: dict[str, str]
    # name -> {name} plus its cross-axis equivalent, for both column values.
    synonyms: dict[str, frozenset[str]]


def _parse_tsv(text: str) -> _CrosswalkMaps:
    """Parse the crosswalk text into its lookup maps.

    Kept separate from resource loading so tests can exercise it with inline
    text. Blank lines and lines starting with '#' (including the column header)
    are ignored. Raises ValueError on a malformed row so a bad edit fails loudly
    rather than silently dropping a rename.
    """
    b2p: dict[str, str] = {}
    p2b: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            raise ValueError(
                f"taxonomy_crosswalk line {lineno}: "
                f"expected 2 tab-separated columns, got {len(parts)}"
            )
        birdnet, perch = parts[0].strip(), parts[1].strip()
        if not birdnet or not perch:
            raise ValueError(f"taxonomy_crosswalk line {lineno}: empty column")
        if birdnet == perch:
            raise ValueError(
                f"taxonomy_crosswalk line {lineno}: self-pair {birdnet!r}"
            )
        if birdnet in b2p:
            raise ValueError(
                f"taxonomy_crosswalk line {lineno}: duplicate BirdNET name {birdnet!r}"
            )
        if perch in p2b:
            raise ValueError(
                f"taxonomy_crosswalk line {lineno}: duplicate Perch name {perch!r}"
            )
        b2p[birdnet] = perch
        p2b[perch] = birdnet

    synonyms: dict[str, set[str]] = {}
    for birdnet, perch in b2p.items():
        pair = {birdnet, perch}
        synonyms.setdefault(birdnet, set()).update(pair)
        synonyms.setdefault(perch, set()).update(pair)
    return _CrosswalkMaps(
        birdnet_to_perch=b2p,
        perch_to_birdnet=p2b,
        synonyms={name: frozenset(eq) for name, eq in synonyms.items()},
    )


@cache
def _warn_unknown_axis(target: str) -> None:
    # to_axis runs once per detection row, so cache the warning to fire once per
    # unknown target per process instead of flooding the log for a whole run.
    _log.warning(
        "taxonomy_crosswalk: unknown target axis %r, leaving names un-normalized "
        "(expected one of %s)",
        target,
        TAXONOMIES,
    )


@cache
def _load_map() -> _CrosswalkMaps:
    text = files(__package__).joinpath("data", _DATA_FILE).read_text(encoding="utf-8")
    return _parse_tsv(text)


def expand_species(names: frozenset[str]) -> frozenset[str]:
    """Add every cross-axis equivalent to a set of scientific names.

    Used to build a species-filter allow-list from user-typed names so an entry
    written in either taxonomy matches whichever model runs. A name with no
    rename expands to just itself.
    """
    maps = _load_map()
    out: set[str] = set(names)
    for name in names:
        out |= maps.synonyms.get(name, frozenset())
    return frozenset(out)


def to_axis(name: str, target: str) -> str:
    """Rewrite one scientific name into the target taxonomy.

    Returns the name unchanged when it is already on the target axis or has no
    rename. Safe regardless of which model emitted the name, because only
    renamed pairs are in the table.

    The crosswalk knows only the two axes in TAXONOMIES. An unknown target
    (e.g. a project that stored a taxonomy a newer build no longer offers)
    passes every name through untouched, so output is left un-normalized. That
    is logged rather than raised so a stale setting degrades to raw model names
    instead of failing a run; adding a third axis means extending this branch.
    """
    maps = _load_map()
    if target == BIRDNET_2_4:
        return maps.perch_to_birdnet.get(name, name)
    if target == PERCH_2_0:
        return maps.birdnet_to_perch.get(name, name)
    _warn_unknown_axis(target)
    return name
