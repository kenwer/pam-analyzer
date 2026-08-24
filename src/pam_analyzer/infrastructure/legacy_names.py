"""Legacy scientific-name aliases for user-authored species lists.

BirdNET v3.0 labels its classes under the taxonomy shared with the geo
model, which splits some genera the older eBird-based axis kept together:
what BirdNET v2.4 called Accipiter gentilis, v3.0 calls Astur gentilis.

Species lists are written by hand and outlive model upgrades, so a list
naming the old spelling would silently stop matching. This module loads a
curated old -> current table (data/legacy_species_aliases.tsv) and expands
user-typed names to cover both spellings.

The mapping applies to user input only. Model output and the geo model's
regional list are already on the current axis, so nothing rewrites them.
This is deliberately one-directional and one-purpose: an earlier version of
this code carried a bidirectional crosswalk because two detectors with two
taxonomies ran side by side, which is no longer the case.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files

_DATA_FILE = "legacy_species_aliases.tsv"


def _parse_tsv(text: str) -> dict[str, str]:
    """Parse the alias table into {legacy_name: current_name}.

    Blank lines and lines starting with '#' are skipped so the committed
    file can carry a provenance header.
    """
    mapping: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            raise ValueError(f"{_DATA_FILE}:{lineno}: expected 2 tab-separated columns, got {len(parts)}")
        legacy, current = (p.strip() for p in parts)
        if legacy in mapping and mapping[legacy] != current:
            raise ValueError(f"{_DATA_FILE}:{lineno}: conflicting alias for {legacy!r}")
        mapping[legacy] = current
    return mapping


@cache
def _load_map() -> dict[str, str]:
    text = files(__package__).joinpath("data", _DATA_FILE).read_text(encoding="utf-8")
    return _parse_tsv(text)


def expand_species(names: frozenset[str]) -> frozenset[str]:
    """Add the current-axis spelling for any legacy name in the set.

    A name with no known alias expands to just itself, so passing a set of
    current-axis names through is a no-op.
    """
    aliases = _load_map()
    out = set(names)
    for name in names:
        current = aliases.get(name)
        if current is not None:
            out.add(current)
    return frozenset(out)
