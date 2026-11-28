"""Regenerate src/pam_analyzer/infrastructure/data/species_aliases.tsv from BirdNET+ Taxonomy.

The app's species namespace is the accepted scientific names of one pinned
BirdNET+ Taxonomy release, the one the newest BirdNET acoustic model was built
against. Every label of every shipped model axis that the taxonomy lists as an
alias of an accepted name becomes one superseded-to-canonical pair. That
includes lumps, where two classes of one model become one species.

Labels the taxonomy does not know stay under the model's own spelling, unless
scripts/species_aliases_supplement.tsv bridges them. The script prints the rest,
so a new bridge can be added when a model or taxonomy update leaves one out.
The supplement also overrides a wrong taxonomy alias, or vetoes it by mapping
a label onto itself.

Reads the model label files from the per-user model folder (or
BIRDNET_APP_DATA), so run `uv run poe prewarm` first. Then:

    uv run python scripts/build_species_aliases.py
"""

from __future__ import annotations

import csv
import hashlib
import io
import sys
import textwrap
import urllib.request
import zipfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pam_analyzer.infrastructure.birdnet_3_0_taxonomy import SOUND_CLASS_GROUPS
from pam_analyzer.infrastructure.birdnet_3_0_taxonomy import labels as v3_0_labels
from pam_analyzer.infrastructure.birdnet_taxonomy import split_sci_common

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "src" / "pam_analyzer" / "infrastructure" / "data" / "species_aliases.tsv"
SUPPLEMENT = Path(__file__).with_name("species_aliases_supplement.tsv")

TAXONOMY_VERSION = "v0.5-Nov2026"
TAXONOMY_URL = (
    "https://github.com/birdnet-team/birdnet-taxonomy/releases/download/"
    f"{TAXONOMY_VERSION}/BirdNET_Taxonomy_{TAXONOMY_VERSION}.zip"
)
TAXONOMY_SHA256 = "6fd24ab69532dcbffa6c53061e55118afe367f94ef06f100f1f386ca5d5421a1"

# The v3.0 developer preview no longer ships, but species lists written for it
# may still name its spellings.
PREVIEW_LABELS_URL = "https://zenodo.org/records/20703646/files/BirdNET+_V3.0-preview3.1_Global_11K_Labels.csv"
PREVIEW_LABELS_SHA256 = "8124b0ea2d187104c5e2cd95a0f937165647e20349c8fd34d4d5ef991821f8f0"


@dataclass(frozen=True)
class Taxonomy:
    accepted: frozenset[str]
    alias_of: dict[str, str]  # alias -> accepted name
    ebird_code: dict[str, str]  # accepted name -> eBird code, where it has one


@dataclass(frozen=True)
class Bridge:
    canonical: str
    ebird_code: str | None = None  # the code the bridge was matched on, re-checked against the taxonomy


def _download(url: str, sha256: str) -> bytes:
    print(f"Downloading {url}", file=sys.stderr)
    with urllib.request.urlopen(url) as response:
        data = response.read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        raise SystemExit(f"{url} has sha256 {actual}, expected {sha256}")
    return data


def parse_taxonomy(csv_text: str) -> Taxonomy:
    """Accepted names and the alias index of one taxonomy CSV.

    Sound classes keep their accepted name but contribute no aliases: the
    taxonomy documents those as search terms, not names.
    """
    accepted: set[str] = set()
    ebird_code: dict[str, str] = {}
    targets: dict[str, set[str]] = defaultdict(set)
    for row in csv.DictReader(io.StringIO(csv_text)):
        name = row["scientific_name"].strip()
        accepted.add(name)
        if row["ebird_code"]:
            ebird_code[name] = row["ebird_code"].strip()
        if row["taxon_group"] in SOUND_CLASS_GROUPS:
            continue
        for alias in (row["scientific_name_aliases"] or "").split("|"):
            alias = alias.strip()
            if alias and alias != name:
                targets[alias].add(name)
    ambiguous = {a: sorted(t) for a, t in targets.items() if len(t) > 1}
    if ambiguous:
        raise SystemExit(f"Aliases naming more than one species: {ambiguous}")
    alias_of = {a: next(iter(t)) for a, t in targets.items() if a not in accepted}
    return Taxonomy(frozenset(accepted), alias_of, ebird_code)


def parse_supplement(text: str) -> dict[str, Bridge]:
    bridges: dict[str, Bridge] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line and not line.startswith("#"):
            fields = [p.strip() for p in line.split("\t")]
            if len(fields) not in (2, 3):
                raise SystemExit(
                    f"{SUPPLEMENT.name} line {number}: expected superseded<TAB>canonical[<TAB>ebird_code], got {raw!r}"
                )
            bridges[fields[0]] = Bridge(*fields[1:])
    return bridges


def derive_pairs(
    taxonomy: Taxonomy, axes: Mapping[str, Iterable[str]], supplement: dict[str, Bridge]
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Pairs for every axis label that is not an accepted name, plus the unresolved labels per axis.

    A supplement entry wins over the taxonomy, so a reviewer can correct it.
    One that maps a label onto itself keeps the model's spelling.
    """
    stale = {
        name: (b.ebird_code, taxonomy.ebird_code.get(b.canonical))
        for name, b in supplement.items()
        if b.ebird_code and taxonomy.ebird_code.get(b.canonical) != b.ebird_code
    }
    if stale:
        raise SystemExit(f"Supplement bridges whose eBird code no longer matches (expected, found): {stale}")

    pairs: dict[str, str] = {}
    unresolved: dict[str, list[str]] = {}
    for axis, names in axes.items():
        missing = []
        for name in names:
            if name in supplement:
                if supplement[name].canonical != name:
                    pairs[name] = supplement[name].canonical
            elif name in taxonomy.accepted:
                continue
            elif name in taxonomy.alias_of:
                pairs[name] = taxonomy.alias_of[name]
            else:
                missing.append(name)
        unresolved[axis] = sorted(set(missing))
    chained = sorted(set(pairs) & set(pairs.values()))
    if chained:
        raise SystemExit(f"Names that are both superseded and canonical: {chained}")
    return dict(sorted(pairs.items())), unresolved


def _split(entries: Iterable[str]) -> list[str]:
    return [split_sci_common(e)[0].strip() for e in entries]


def _preview_labels() -> list[str]:
    text = _download(PREVIEW_LABELS_URL, PREVIEW_LABELS_SHA256).decode("utf-8-sig")
    return [row["sci_name"].strip() for row in csv.DictReader(io.StringIO(text), delimiter=";") if row["sci_name"]]


def model_axes() -> dict[str, Callable[[], list[str]]]:
    # Lazy, because both import birdnet at module load, which tests of this script do not need
    from pam_analyzer.infrastructure import birdnet_2_4_onnx, perch_onnx

    return {
        "BirdNET v3.0": lambda: _split(v3_0_labels("en_us")),
        "BirdNET v3.0 geo": lambda: _split(v3_0_labels("en_us", "geo")),
        "BirdNET v2.4": lambda: _split(birdnet_2_4_onnx.labels("acoustic", "en_us")),
        "BirdNET v2.4 geo": lambda: _split(birdnet_2_4_onnx.labels("geo", "en_us")),
        "Perch 2.0": lambda: list(perch_onnx.labels()),
        "BirdNET v3.0 preview (retired)": _preview_labels,
    }


def render(pairs: dict[str, str], axes: dict[str, list[str]]) -> str:
    lumps = sum(1 for count in Counter(pairs.values()).values() if count > 1)
    axes_note = textwrap.fill(
        f"{len(pairs)} pairs over the labels of {', '.join(axes)}.", width=78, initial_indent="# ", subsequent_indent="# "
    )
    header = f"""\
# Scientific-name supersessions: a spelling that is no longer current, and the
# spelling this app writes instead. One direction only, superseded to canonical.
#
# Columns: superseded_name<TAB>canonical_name
#
# Generated by scripts/build_species_aliases.py from BirdNET+ Taxonomy
# {TAXONOMY_VERSION} (https://github.com/birdnet-team/birdnet-taxonomy), whose
# accepted names are this app's namespace, and from
# scripts/species_aliases_supplement.tsv, which bridges genus moves the
# taxonomy lists no alias for and vetoes wrong aliases. Do not edit by hand.
#
{axes_note}
# {lumps} canonical names have more than one superseded spelling.
#
# Derived from BirdNET+ Taxonomy, licensed CC BY-SA 4.0
# (https://creativecommons.org/licenses/by-sa/4.0/). This file is shared under
# the same licence.
"""
    return header + "".join(f"{s}\t{c}\n" for s, c in pairs.items())


def main() -> int:
    archive = zipfile.ZipFile(io.BytesIO(_download(TAXONOMY_URL, TAXONOMY_SHA256)))
    csv_text = archive.read(f"BirdNET_Taxonomy_{TAXONOMY_VERSION}.csv").decode("utf-8-sig")
    taxonomy = parse_taxonomy(csv_text)
    axes = {name: read() for name, read in model_axes().items()}
    supplement = parse_supplement(SUPPLEMENT.read_text(encoding="utf-8"))

    pairs, unresolved = derive_pairs(taxonomy, axes, supplement)
    OUTPUT.write_text(render(pairs, axes), encoding="utf-8")

    print(f"Wrote {len(pairs)} pairs to {OUTPUT.relative_to(REPO_ROOT)}", file=sys.stderr)
    for axis, names in unresolved.items():
        print(f"\n{axis}: {len(names)} labels not in the taxonomy, kept as the model spells them", file=sys.stderr)
        for name in names:
            print(f"  {name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
