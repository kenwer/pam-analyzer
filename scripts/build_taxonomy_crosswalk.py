"""Regenerate the BirdNET-2.4 <-> Perch-2.0 scientific-name crosswalk.

The two detectors label their classes under different taxonomies (BirdNET on
eBird 2021, Perch on iNaturalist 2024). A genus split renames some classes, e.g.
BirdNET's 'Accipiter gentilis' is Perch's 'Astur gentilis'. The app bridges those
renames in src/pam_analyzer/infrastructure/data/taxonomy_crosswalk.tsv. Names
both axes spell identically need no row (the loader passes them through), so this
file holds only the renamed pairs.

There is no clean algorithmic identity key between the two axes: Perch ships no
common names, and its eBird-code file blanks the code ('no_ebird_code') for
exactly the renamed classes. So the crosswalk cannot be derived purely from the
shipped files, and part of it is human judgement. This script combines three
signals and one curated block, and every emitted pair is validated against the
live label axes:

  1. Systematic genus shift. A real split renames a whole genus, so a
     (birdnet_genus -> perch_genus) pair recurs across many shared epithets. We
     score each genus-pair and, per BirdNET name, take the strongest unique one.
     This resolves the bulk (e.g. Charadrius -> Anarhynchus, Ixobrychus ->
     Botaurus) without per-bird judgement.
  2. Gender-flipped epithet. A split that also changed the epithet ending
     (Accipiter virgatus -> Tachyspiza virgata) shares no final token, so the
     epithet match misses it. Within the genus-pairs pass 1 already established,
     we match on a gender-neutral stem and keep unambiguous hits.
  3. Curated overrides (CURATED below). One-off moves the frequency test cannot
     confirm, each verified by hand from the BirdNET common name plus a related
     target genus, with distractors rejected via their eBird codes. Persisted
     here so a plain re-run reproduces the reviewed table.

Anything with an epithet candidate that none of the above resolves is printed to
stderr as REVIEW, so a future model-version bump surfaces new candidates for a
human instead of silently changing the table.

Usage:
    uv run python scripts/build_taxonomy_crosswalk.py            # print to stdout
    uv run python scripts/build_taxonomy_crosswalk.py -o out.tsv # write a file

Loading the Perch axis downloads the pinned Perch v2 model on a cold cache, and
the BirdNET axis downloads the geo model's label files, so the first run is slow.
Regenerate and re-verify whenever the BirdNET version or PERCH_V2_KAGGLE_VERSION
(see src/pam_analyzer/infrastructure/model_versions.py) changes.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict

from pam_analyzer.infrastructure.birdnet_lib import (
    locale_label_map,
    perch_species_scientific,
)

MIN_PAIR = 2  # shared epithets that make a genus-pair count as systematic

# One-off renames the frequency test cannot confirm, verified by hand (see
# module docstring, signal 3). Keyed birdnet_scientific -> perch_scientific.
# Includes two non-bird renames (treefrogs) that both axes carry; those are
# deliberate, the crosswalk is not bird-only.
CURATED = {
    "Accipiter superciliosus": "Microspizias superciliosus",
    "Accipiter tachiro": "Aerospiza tachiro",
    "Alophoixus finschii": "Iole finschii",
    "Amblyornis newtoniana": "Prionodura newtoniana",
    "Apus melba": "Tachymarptis melba",
    "Bubo lacteus": "Ketupa lactea",
    "Bubo nipalensis": "Ketupa nipalensis",
    "Bubo sumatranus": "Ketupa sumatrana",
    "Bubulcus ibis": "Ardea ibis",
    "Buceros vigil": "Rhinoplax vigil",
    "Cacomantis leucolophus": "Caliechthrus leucolophus",
    "Cacomantis pallidus": "Heteroscenes pallidus",
    "Calocitta colliei": "Cyanocorax colliei",
    "Calocitta formosa": "Cyanocorax formosus",
    "Calyptorhynchus funereus": "Zanda funerea",
    "Calyptorhynchus latirostris": "Zanda latirostris",
    "Charadrius modestus": "Zonibyx modestus",
    "Charadrius morinellus": "Eudromias morinellus",
    "Cholornis unicolor": "Paradoxornis unicolor",
    "Clytolaema rubricauda": "Heliodoxa rubricauda",
    "Columba larvata": "Aplopelia larvata",
    "Coracina cinerea": "Ceblepyris cinereus",
    "Coracina pectoralis": "Ceblepyris pectoralis",
    "Cracticus quoyi": "Melloria quoyi",
    "Cranioleuca gutturata": "Thripophaga gutturata",
    "Cyornis hoevelli": "Eumyias hoevelli",
    "Dinopium rafflesii": "Gecinulus rafflesii",
    "Dryophytes andersonii": "Hyla andersonii",
    "Dryotriorchis spectabilis": "Circaetus spectabilis",
    "Elseyornis melanops": "Thinornis melanops",
    "Eupodotis ruficrista": "Lophotis ruficrista",
    "Eupodotis vigorsii": "Heterotetrax vigorsii",
    "Hapalocrex flaviventer": "Laterallus flaviventer",
    "Herpsilochmus sellowi": "Radinopsyche sellowi",
    "Hydropsalis maculicaudus": "Antiurus maculicaudus",
    "Hyliola regilla": "Pseudacris regilla",
    "Lophochroa leadbeateri": "Cacatua leadbeateri",
    "Lybius bidentatus": "Pogonornis bidentatus",
    "Melaenornis silens": "Sigelus silens",
    "Micropygia schomburgkii": "Rufirallus schomburgkii",
    "Mirafra rufocinnamomea": "Amirafra rufocinnamomea",
    "Musophaga rossae": "Tauraco rossae",
    "Nyctibius bracteatus": "Phyllaemulor bracteatus",
    "Otocichla mupinensis": "Turdus mupinensis",
    "Pitangus lictor": "Philohydor lictor",
    "Psephotus varius": "Psephotellus varius",
    "Pseudeos cardinalis": "Chalcopsitta cardinalis",
    "Psophocichla litsitsirupa": "Turdus litsitsirupa",
    "Rhodothraupis celaeno": "Periporphyrus celaeno",
    "Sakesphorus cristatus": "Sakesphoroides cristatus",
    "Sericornis citreogularis": "Neosericornis citreogularis",
    "Sinosuthora webbiana": "Suthora webbiana",
    "Systellura decussata": "Quechuavis decussata",
    "Tauraco porphyreolophus": "Gallirex porphyreolophus",
    "Tregellasia capito": "Eopsaltria capito",
    "Tumbezia salvini": "Ochthoeca salvini",
    "Urosphena pallidipes": "Hemitesia pallidipes",
    "Vanellus cayanus": "Hoploxypterus cayanus",
}


def _genus(name: str) -> str:
    return name.split(" ", 1)[0]


def _epithet(name: str) -> str:
    return name.rsplit(" ", 1)[-1].lower()


def _stem(epithet: str) -> str:
    """Strip a trailing Latin gender ending so virgatus and virgata unify."""
    for suffix in ("us", "um", "is", "a", "e", "i"):
        if epithet.endswith(suffix) and len(epithet) - len(suffix) >= 3:
            return epithet[: -len(suffix)]
    return epithet


def _load_axes() -> tuple[dict[str, str], frozenset[str]]:
    """Return ({birdnet_scientific: common}, {perch_scientific}).

    BirdNET common names come from the en_us geo labels and are used only to
    annotate REVIEW output. Perch names come straight off the model's label
    file. Both trigger a model or label download on a cold cache.
    """
    birdnet_common = locale_label_map("en_us")
    return birdnet_common, perch_species_scientific()


def _candidates(
    birdnet: frozenset[str], perch: frozenset[str]
) -> tuple[list[tuple[str, list[str]]], dict[str, list[str]]]:
    """Per BirdNET name absent from Perch, its exact-epithet Perch candidates.

    Also returns the epithet index of Perch names Perch renamed away from
    BirdNET, reused by the gender-stem pass.
    """
    perch_by_epithet: dict[str, list[str]] = defaultdict(list)
    for name in perch:
        if name not in birdnet:
            perch_by_epithet[_epithet(name)].append(name)
    rows = []
    for bn in sorted(birdnet):
        if bn in perch:
            continue
        rows.append((bn, sorted(perch_by_epithet.get(_epithet(bn), []))))
    return rows, perch_by_epithet


def _systematic(rows: list[tuple[str, list[str]]]) -> tuple[dict[str, str], set[tuple[str, str]]]:
    """Resolve each BirdNET name to the strongest recurring genus-pair.

    Returns the resolved {birdnet: perch} map and the set of systematic
    (birdnet_genus, perch_genus) pairs, which the gender-stem pass reuses.
    """
    pair_count: Counter[tuple[str, str]] = Counter()
    for bn, cands in rows:
        g = _genus(bn)
        for p in cands:
            pair_count[(g, _genus(p))] += 1
    systematic_pairs = {gp for gp, n in pair_count.items() if n >= MIN_PAIR}

    resolved: dict[str, str] = {}
    for bn, cands in rows:
        if not cands:
            continue
        g = _genus(bn)
        scored = sorted(((pair_count[(g, _genus(p))], p) for p in cands), reverse=True)
        top, top_p = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0
        if top >= MIN_PAIR and top > second:
            resolved[bn] = top_p
    return resolved, systematic_pairs


def _gender_flips(
    birdnet: frozenset[str],
    perch: frozenset[str],
    systematic_pairs: set[tuple[str, str]],
    already: set[str],
) -> dict[str, str]:
    """Catch gender-flipped epithets within already-systematic genus-pairs.

    Only fires for BirdNET names not resolved by the systematic pass, and only
    accepts an unambiguous stem hit, so it cannot overrule a stronger signal.
    """
    genus_targets: dict[str, set[str]] = defaultdict(set)
    for g, h in systematic_pairs:
        genus_targets[g].add(h)
    perch_by_genus_stem: dict[tuple[str, str], list[str]] = defaultdict(list)
    for name in perch:
        perch_by_genus_stem[(_genus(name), _stem(_epithet(name)))].append(name)

    out: dict[str, str] = {}
    for bn in sorted(birdnet):
        if bn in perch or bn in already:
            continue
        ep = _epithet(bn)
        st = _stem(ep)
        hits = set()
        for h in genus_targets.get(_genus(bn), ()):
            for p in perch_by_genus_stem.get((h, st), []):
                if _epithet(p) != ep:  # exact-epithet is the systematic pass's job
                    hits.add(p)
        if len(hits) == 1:
            out[bn] = next(iter(hits))
    return out


def build_table() -> tuple[
    list[tuple[str, str]], list[str], list[tuple[str, str, list[str]]]
]:
    """Return (validated pairs, warnings, review rows).

    Pairs are sorted and each is guaranteed to have both names on their axis and
    to differ. Warnings flag curated entries that no longer validate (a stale
    override after a version bump). Review rows are unresolved epithet candidates.
    """
    birdnet_common, perch = _load_axes()
    birdnet = frozenset(birdnet_common)
    rows, _ = _candidates(birdnet, perch)

    resolved, systematic_pairs = _systematic(rows)
    resolved.update(_gender_flips(birdnet, perch, systematic_pairs, set(resolved)))
    # Curated wins over the automatic passes (human corrections and gap-fills).
    resolved.update(CURATED)

    pairs: list[tuple[str, str]] = []
    warnings: list[str] = []
    for bn, p in resolved.items():
        if bn not in birdnet:
            warnings.append(f"drop: birdnet name off axis: {bn!r}")
        elif p not in perch:
            warnings.append(f"drop: perch name off axis: {bn!r} -> {p!r}")
        elif bn == p:
            warnings.append(f"drop: self-pair: {bn!r}")
        else:
            pairs.append((bn, p))
    pairs.sort()

    review = [
        (bn, birdnet_common.get(bn, ""), cands)
        for bn, cands in rows
        if cands and bn not in resolved
    ]
    return pairs, warnings, review


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o", "--output", help="write TSV here instead of stdout", default=None
    )
    args = parser.parse_args()

    pairs, warnings, review = build_table()

    header = (
        "# BirdNET-2.4 (eBird 2021) <-> Perch-2.0 (iNaturalist 2024) scientific-name renames.\n"
        "# Two tab-separated columns: birdnet_scientific<TAB>perch_scientific.\n"
        "# Only genus/name renames belong here (a class both axes carry under different\n"
        "# names). Names shared verbatim by both axes are omitted, and BirdNET classes\n"
        "# that Perch genuinely lacks (no equivalent) are not renames and are omitted too.\n"
        "#\n"
        "# Generated by scripts/build_taxonomy_crosswalk.py: systematic genus shift plus a\n"
        "# gender-neutral epithet-stem pass plus a curated block of hand-verified one-off\n"
        "# moves. Re-verify by hand if the BirdNET version or PERCH_V2_KAGGLE_VERSION\n"
        "# (see infrastructure/model_versions.py) changes.\n"
        "#\n"
        "# birdnet_scientific\tperch_scientific\n"
    )
    body = "".join(f"{bn}\t{p}\n" for bn, p in pairs)
    text = header + body

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)

    for w in warnings:
        print(f"# WARNING {w}", file=sys.stderr)
    print(f"# {len(pairs)} rename pairs emitted.", file=sys.stderr)
    if review:
        print(
            f"# REVIEW: {len(review)} BirdNET names have an epithet candidate no "
            "pass resolved (verify by hand, add to CURATED if a real rename):",
            file=sys.stderr,
        )
        for bn, common, cands in review:
            print(f"#   {bn} ({common})\t-> {', '.join(cands)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
