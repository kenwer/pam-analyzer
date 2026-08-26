"""Species identity: one namespace for the labels of every model.

A scientific name is only meaningful relative to the model that emitted it.
BirdNET v2.4 calls the Eurasian Goshawk Accipiter gentilis and v3.0 calls it
Astur gentilis, and v3.0's label file carries both Charadrius dubius and
Thinornis dubius as separate classes for the Little Ringed Plover. Comparing
raw labels across those sources is therefore unsound.

This module defines the one namespace the rest of the app works in. The rule
is: use the model's own label, except where the shipped table says a spelling
has been superseded. It is not "BirdNET v3.0's taxonomy". The table happens to
hold v2.4-to-v3.0 renames because that is the only divergence that has
existed, and a supersession from any source belongs in it. Names nothing has
an opinion about, such as Perch's insects and sound events, pass through
untouched.

canonical() is applied at the boundary where a label enters the app, so no
code downstream needs to know which model produced a name. See
docs/one-species-namespace.md for why the previous design, which rewrote
names on the way out instead, could not be correct in both directions at
once.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import cache
from importlib.resources import files

_DATA_FILE = "species_aliases.tsv"


def _parse_tsv(text: str) -> dict[str, str]:
    """Parse the alias table into {superseded_name: canonical_name}.

    Blank lines and lines starting with '#' are skipped so the committed file
    can carry a provenance header.

    Raises ValueError if the two name spaces overlap, i.e. if some name is the
    superseded spelling of one pair and the canonical spelling of another.
    Such a chain would make canonical() depend on how many times it ran, so it
    fails loudly here rather than producing a name in neither space.
    """
    mapping: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            raise ValueError(f"{_DATA_FILE}:{lineno}: expected 2 tab-separated columns, got {len(parts)}")
        superseded, current = (p.strip() for p in parts)
        if superseded in mapping and mapping[superseded] != current:
            raise ValueError(f"{_DATA_FILE}:{lineno}: conflicting alias for {superseded!r}")
        mapping[superseded] = current

    chained = sorted(set(mapping) & set(mapping.values()))
    if chained:
        raise ValueError(f"{_DATA_FILE}: name is both a superseded and a canonical spelling: {chained}")
    return mapping


@cache
def _load_map() -> dict[str, str]:
    text = files(__package__).joinpath("data", _DATA_FILE).read_text(encoding="utf-8")
    return _parse_tsv(text)


def canonical(name: str) -> str:
    """The one name this app knows the given model label by.

    Total: a name with no recorded supersession is returned unchanged, so a
    caller can apply this to any label without knowing which model it came
    from. Idempotent, because _parse_tsv rejects chained renames.
    """
    return _load_map().get(name, name)


def canonical_set(names: Iterable[str]) -> frozenset[str]:
    """canonical() over a collection.

    The result can be smaller than the input. Two labels of one model that
    denote the same bird collapse to one member, which is the point.
    """
    return frozenset(canonical(n) for n in names)
