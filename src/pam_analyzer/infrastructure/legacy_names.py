"""Scientific-name aliases between BirdNET v2.4's axis and v3.0's.

BirdNET v3.0 labels its classes under the taxonomy shared with the geo
model, which splits some genera the older eBird-based axis kept together:
what BirdNET v2.4 called Accipiter gentilis, v3.0 calls Astur gentilis.
The curated table in data/legacy_species_aliases.tsv holds those 175 pairs.

The table is used in two different ways, and keeping them straight matters:

- to_axis() runs one direction per call, chosen by the project's taxonomy
  setting. Each runner applies it to its own model output so every
  detection in a project is written under one axis and the two engines'
  rows line up in the Examine grid, whichever engine produced them.
- expand_species() runs both directions at once. Species lists are written
  by hand and outlive model upgrades, so a name typed under either axis has
  to match whichever engine the user runs. Expanding a set is safe in both
  directions because a spelling the running model does not emit simply
  never matches.

Only renamed pairs are in the table. Names both axes spell identically are
absent, so a rewrite of such a name is a no-op in either direction.
"""

from __future__ import annotations

import logging
from functools import cache
from importlib.resources import files

_log = logging.getLogger(__name__)

# Scientific-name axes a project can normalize its output to, in UI-display
# order. The default axis comes first so a combo that cannot find a stored
# value falls back to it by selecting index 0.
#
# These name model generations, not model releases. BIRDNET_3_0 stays
# correct when the preview build gives way to a final v3.0 and the runner's
# model_key changes. The strings are persisted in project.toml, so treat
# them as an on-disk format and do not rename them.
BIRDNET_3_0 = "BirdNET-3.0"
BIRDNET_2_4 = "BirdNET-2.4"
TAXONOMIES = (BIRDNET_3_0, BIRDNET_2_4)

_DATA_FILE = "legacy_species_aliases.tsv"


def _parse_tsv(text: str) -> dict[str, str]:
    """Parse the alias table into {legacy_name: current_name}.

    Blank lines and lines starting with '#' are skipped so the committed
    file can carry a provenance header.

    Raises ValueError if the two name spaces overlap, i.e. if some name is
    the legacy spelling of one pair and the current spelling of another.
    Such a chain would make a rewrite depend on the order rows are applied
    in, so it fails loudly here rather than producing a name that belongs to
    neither axis.
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

    chained = sorted(set(mapping) & set(mapping.values()))
    if chained:
        raise ValueError(f"{_DATA_FILE}: name is both a legacy and a current spelling: {chained}")
    return mapping


@cache
def _load_map() -> dict[str, str]:
    text = files(__package__).joinpath("data", _DATA_FILE).read_text(encoding="utf-8")
    return _parse_tsv(text)


@cache
def _load_reverse_map() -> dict[str, frozenset[str]]:
    """{current_name: {legacy spellings}} for input expansion.

    A set of legacy names per current name rather than a single string. The
    table happens to be one-to-one today, but a future genus split could
    merge two legacy names onto one current name, and silently keeping only
    the last one would drop a spelling users still have in their lists.
    """
    reverse: dict[str, set[str]] = {}
    for legacy, current in _load_map().items():
        reverse.setdefault(current, set()).add(legacy)
    return {current: frozenset(legacy) for current, legacy in reverse.items()}


@cache
def _load_current_to_legacy() -> dict[str, str]:
    """{current_name: legacy_name} for rewriting output onto the v2.4 axis.

    Single-valued, unlike _load_reverse_map. Writing a name to CSV has to
    pick exactly one spelling. A current name with several legacy spellings
    would have no defensible choice, so it raises instead of picking one.
    That cannot happen with today's one-to-one table and would only arise
    from a future edit, which is when the error is useful.
    """
    out: dict[str, str] = {}
    for current, legacy_names in _load_reverse_map().items():
        if len(legacy_names) > 1:
            raise ValueError(
                f"{_DATA_FILE}: {current!r} has several legacy spellings "
                f"{sorted(legacy_names)}, cannot rewrite onto the "
                f"{BIRDNET_2_4} axis unambiguously"
            )
        out[current] = next(iter(legacy_names))
    return out


@cache
def _warn_unknown_axis(target: str) -> None:
    # to_axis runs once per detection row, so cache the warning to fire once per unknown target per process instead of flooding the log for a run
    _log.warning(
        "legacy_names: unknown target axis %r, leaving names un-normalized (expected one of %s)",
        target,
        TAXONOMIES,
    )


def to_axis(name: str, target: str) -> str:
    """Rewrite one scientific name into the target taxonomy.

    Returns the name unchanged when it is already on the target axis or has
    no alias, so a runner can apply this to its own output unconditionally
    without knowing which axis its model emits. The two name spaces are
    disjoint (enforced in _parse_tsv), so neither direction can pick up a
    name the other direction just wrote.

    An unknown target, e.g. a project.toml carrying a taxonomy this build no
    longer offers, passes every name through untouched and leaves output on
    the model's own axis. That is logged rather than raised so a stale
    setting degrades to raw model names instead of failing a whole run.
    """
    if target == BIRDNET_3_0:
        return _load_map().get(name, name)
    if target == BIRDNET_2_4:
        return _load_current_to_legacy().get(name, name)
    _warn_unknown_axis(target)
    return name


def expand_species(names: frozenset[str]) -> frozenset[str]:
    """Add every known spelling of each name, on both axes.

    A name with no known alias expands to just itself. Both directions are
    covered so a must-have list typed under either taxonomy matches whichever
    model runs. A v2.4 run checks names against v2.4's axis, a v3.0 run
    against v3.0's, and the expanded set carries the spelling each needs.
    This is independent of the project's output taxonomy, which decides only
    what gets written, not what matches.
    """
    aliases = _load_map()
    reverse = _load_reverse_map()
    out = set(names)
    for name in names:
        current = aliases.get(name)
        if current is not None:
            out.add(current)
        out.update(reverse.get(name, ()))
    return frozenset(out)
