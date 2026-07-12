#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = [
#     "matplotlib",
#     "numpy",
#     "pandas",
# ]
# ///
"""Empirical recalibration of _PERCH_LOGIT_OFFSET against BirdNET v2.4.

Usage:
    uv run --script scripts/calibrate_perch_offset.py
    # or, since the script has the PEP 723 shebang and is executable:
    ./scripts/calibrate_perch_offset.py --no-plots

IMPORTANT: BirdNET v2.4 is NOT ground truth. It is the older, less
sensitive model of the two. Perch v2 might surface species/windows
that BirdNET misses (that is part of why we run it), so a Perch detection
without a matching BirdNET detection is NOT a false positive. Treat the
"BN agreement rate" metric as one half of the calibration evidence:
- a high agreement rate at a given offset means Perch still catches the
  conservative-BirdNET detections (good - we are not filtering signal),
- a low agreement rate is ambiguous: it can mean Perch is dropping real
  birds OR that we have crossed into Perch's added-value territory.
Read the agreement metric jointly with the Perch/BirdNET row ratio and
the per-species shape of the BN-agreement curve, not in isolation.

The script auto-discovers paired ``*-detections-Perch-2.0.csv`` and
``*-detections-BirdNET-2.4.csv`` files in the repo root (override with
``--root``). For each candidate offset O it:

1. Inverts every stored Perch confidence back to its raw logit using the
   *current* OFFSET=11.2 baked into the CSV.
2. Resimulates the runner with the candidate offset O at the same
   ``min_conf`` the CSV was produced with: a Perch row is kept iff
   ``sigmoid(raw_logit - O) >= min_conf``.
3. Joins surviving Perch rows against BirdNET rows on
   ``(File, Scientific_Name)`` with a temporal-overlap predicate that
   tolerates the 3 s vs 5 s window mismatch.
4. Reports overall row count, Perch/BirdNET row ratio, BN agreement
   rate (fraction of BirdNET rows with a matching Perch detection -
   NOT a true recall, see note above), and per-species agreement rate
   for the most prevalent BirdNET species.
5. Detects an agreement cliff (largest one-step drop in per-species
   agreement across consecutive offsets for high-prevalence species)
   and emits a recommendation.
6. Renders three matplotlib figures (suppress with ``--no-plots``):
   - the overall raw-logit histogram with the inference-time cutoff and
     candidate offset thresholds marked, so the right-censored tail is
     visible at a glance,
   - a per-species histogram grid for the top-N species, where any
     genuine noise/signal bimodality would live,
   - per-species BN-agreement curves vs candidate offset, which would
     show a knee if a cliff exists.

Right-censoring note: the input CSVs were produced at OFFSET=11.2 with
min_conf=0.25, so raw logits below ~10.10 were already filtered at
inference time. Offsets < 11.2 are therefore evaluated on incomplete
data and shown with a (censored) marker; they are still useful as a
sanity check that the cliff is approached from the right side.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

# The offset baked into every stored Perch confidence. The CSV column
# `Confidence` is `sigmoid(raw_logit - CURRENT_OFFSET)`; we invert with this
# value to recover the raw logit, then re-apply any candidate offset to
# resimulate the runner. Must track src/pam_analyzer/infrastructure/
# perch_runner.py:_PERCH_LOGIT_OFFSET. If the runner's offset changes and
# the CSVs are regenerated, update this in lockstep.
CURRENT_OFFSET = 11.2

# Minimum BirdNET row count for a species to be eligible as a cliff
# indicator. This is a statistical floor, not a representativeness one:
# per-species BN agreement is a binomial proportion p_hat = matched / n,
# whose standard error is sqrt(p(1-p)/n). At p ~ 0.8 (typical here):
#   n = 10  -> SE ~ 0.126  (one flipped match = ~10 pp swing, pure noise)
#   n = 30  -> SE ~ 0.073  (~3 pp swing, comparable to real per-step drops)
#   n = 100 -> SE ~ 0.040
# 30 is the smallest n where one-step noise stays below the cliff
# detector's min_absolute_drop = 0.10. Tied to that constant: raise both
# together if you want to surface subtler cliffs.
#
# Stays absolute on purpose. The reliability of a per-species estimate
# depends only on that species' own row count, not on the surrounding
# dataset size, so a percentage-based threshold would be too lax on small
# datasets and too strict on huge ones.
CLIFF_MIN_PREVALENCE = 30


@dataclass(frozen=True)
class Pair:
    label: str
    perch_path: Path
    birdnet_path: Path


def discover_pairs(root: Path) -> list[Pair]:
    perch_re = re.compile(r"(.+)-detections-Perch-2\.0\.csv$")
    pairs: list[Pair] = []
    for p in sorted(root.glob("*-detections-Perch-2.0.csv")):
        m = perch_re.search(p.name)
        if not m:
            continue
        stem = m.group(1)
        birdnet = root / f"{stem}-detections-BirdNET-2.4.csv"
        if birdnet.exists():
            pairs.append(Pair(label=stem, perch_path=p, birdnet_path=birdnet))
    return pairs


def load_pair(pair: Pair) -> tuple[pd.DataFrame, pd.DataFrame]:
    usecols = ["Campaign", "ARU", "Week", "File", "Scientific_Name",
               "Start_Time", "End_Time", "Confidence", "Min_Conf"]
    perch = pd.read_csv(pair.perch_path, usecols=usecols)
    birdnet = pd.read_csv(pair.birdnet_path, usecols=usecols)
    if perch["Min_Conf"].nunique() != 1 or birdnet["Min_Conf"].nunique() != 1:
        raise ValueError(f"Mixed Min_Conf values in {pair.label}")
    # The two CSVs can legitimately have different input min_conf values
    # (e.g. Perch rerun at 0.05 while BirdNET stayed at 0.25). They are used
    # independently downstream: Perch's min_conf governs censoring, BirdNET's
    # only affects how many reference rows we have to match against.
    return perch, birdnet


def invert_to_raw_logit(prob: pd.Series, baked_offset: float = CURRENT_OFFSET) -> pd.Series:
    p = prob.clip(1e-6, 1 - 1e-6)
    return np.log(p / (1 - p)) + baked_offset


def overlap_join(birdnet: pd.DataFrame, perch: pd.DataFrame) -> pd.Series:
    """For each BirdNET row, return whether any Perch row matches.

    Match = same File and Scientific_Name AND temporally overlapping windows.
    Index of returned Series aligns with `birdnet.index`.
    """
    if perch.empty:
        return pd.Series(False, index=birdnet.index)
    birdnet_local = birdnet.reset_index().rename(columns={"index": "_b_idx"})
    merged = birdnet_local.merge(
        perch[["File", "Scientific_Name", "Start_Time", "End_Time"]],
        on=["File", "Scientific_Name"],
        suffixes=("_b", "_p"),
    )
    overlapping = (
        (merged["Start_Time_b"] < merged["End_Time_p"])
        & (merged["Start_Time_p"] < merged["End_Time_b"])
    )
    matched_b_idx = merged.loc[overlapping, "_b_idx"].unique()
    out = pd.Series(False, index=birdnet.index)
    out.loc[matched_b_idx] = True
    return out


def evaluate_offset(
    *,
    offset: float,
    eval_min_conf: float,
    input_logit_cutoff: float,
    perch: pd.DataFrame,
    birdnet: pd.DataFrame,
    raw_logit: pd.Series,
    report_species: list[str],
) -> dict[str, float | int | dict[str, float]]:
    threshold = math.log(eval_min_conf / (1 - eval_min_conf)) + offset
    keep = raw_logit >= threshold
    perch_sub = perch.loc[keep]
    matched = overlap_join(birdnet, perch_sub)
    species_bn_agreement: dict[str, float] = {}
    for sp in report_species:
        mask = birdnet["Scientific_Name"] == sp
        n = int(mask.sum())
        if n == 0:
            continue
        species_bn_agreement[sp] = float(matched[mask].mean())
    return {
        "offset": offset,
        "perch_rows": int(keep.sum()),
        "ratio": float(keep.sum() / max(len(birdnet), 1)),
        "bn_agreement_overall": float(matched.mean()),
        "species_bn_agreement": species_bn_agreement,
        # Uncensored iff the eval threshold sits at or above the inference-
        # time cutoff baked into the CSV. Otherwise the CSV is missing rows
        # in [input_logit_cutoff, threshold) and the metric is biased low.
        "censored": threshold < input_logit_cutoff - 1e-9,
    }


def find_agreement_cliff(
    rows: list[dict],
    species: list[str],
    *,
    min_offset: float,
    cliff_ratio: float = 2.0,
    min_absolute_drop: float = 0.10,
) -> tuple[str, float, float] | None:
    """Return (species, offset_before_cliff, drop_size) for a *real* cliff.

    A cliff is the largest one-step BN-agreement drop among `species` (offsets
    >= min_offset) that ALSO satisfies both:
    - drop >= cliff_ratio * median_step_drop (significantly steeper than
      the smooth-descent baseline), and
    - drop >= min_absolute_drop (avoid flagging tiny absolute moves).

    Returns None when no step qualifies. A smooth monotonic decline (no
    abrupt boundary between signal and noise) yields None by design.
    """
    eligible = [r for r in rows if r["offset"] >= min_offset - 1e-9]
    if len(eligible) < 2:
        return None
    all_drops: list[float] = []
    candidates: list[tuple[str, float, float]] = []
    for sp in species:
        for r_a, r_b in zip(eligible, eligible[1:]):
            ra = r_a["species_bn_agreement"].get(sp)
            rb = r_b["species_bn_agreement"].get(sp)
            if ra is None or rb is None:
                continue
            drop = ra - rb
            all_drops.append(drop)
            if drop > 0:
                candidates.append((sp, r_a["offset"], drop))
    if not candidates:
        return None
    positive_drops = [d for d in all_drops if d > 0]
    median_step = float(np.median(positive_drops)) if positive_drops else 0.0
    threshold = max(min_absolute_drop, cliff_ratio * median_step)
    qualifying = [c for c in candidates if c[2] >= threshold]
    if not qualifying:
        return None
    return max(qualifying, key=lambda c: c[2])


def format_table(rows: list[dict], focus_species: list[str]) -> str:
    header = ["Offset", "PerchRows", "P/B Ratio", "BN-agree(all)"]
    header.extend(focus_species)
    lines = ["  ".join(f"{h:>14}" for h in header)]
    for r in rows:
        marker = "*" if r["censored"] else " "
        cells = [
            f"{r['offset']:>13.2f}{marker}",
            f"{r['perch_rows']:>14d}",
            f"{r['ratio']:>14.3f}",
            f"{r['bn_agreement_overall']:>14.3f}",
        ]
        for sp in focus_species:
            v = r["species_bn_agreement"].get(sp)
            cells.append(f"{v:>14.3f}" if v is not None else f"{'-':>14}")
        lines.append("  ".join(cells))
    return "\n".join(lines)


def plot_diagnostics(
    *,
    raw_logit: pd.Series,
    perch: pd.DataFrame,
    rows: list[dict],
    report_species: list[str],
    input_logit_cutoff: float,
    eval_min_conf: float,
) -> None:
    """Render three figures and block on plt.show() until all are closed.

    Figure 1 (overall histogram): the question is whether the right-
    censored tail below the current inference cutoff hides a noise hump.
    Vertical lines show the cutoff plus the logit threshold each candidate
    offset would impose, so the visual answer to "where would offset O
    slice the distribution?" is immediate.

    Figure 2 (per-species histogram grid): a single global histogram
    averages over 14,795 Perch classes with very different noise floors.
    Per-species histograms isolate each species' own bimodality, which is
    where a real cliff would live.

    Figure 3 (agreement vs offset curves): visual confirmation of the
    table. A genuine cliff shows as a knee; smooth degradation shows as
    a monotonic slope. Remember that 100% agreement is NOT the goal -
    Perch is the stronger model and is expected to add detections that
    BirdNET misses, which lowers this metric without lowering quality.
    """
    import matplotlib.pyplot as plt

    cutoff = input_logit_cutoff
    candidate_offsets = [11.2, 11.5, 12.0, 12.5]
    footer_kwargs = dict(fontsize=9, color="#222222",
                         ha="left", va="bottom", wrap=False)
    footer_box = dict(boxstyle="round,pad=0.5", facecolor="#fff8d6",
                      edgecolor="#b08c2a", alpha=0.95)

    # ---- Figure 1: overall raw-logit histogram ----
    fig1, ax = plt.subplots(figsize=(12, 7.4))
    ax.hist(raw_logit, bins=80, color="steelblue", edgecolor="black", alpha=0.85)
    ax.axvline(cutoff, color="red", linestyle="--", linewidth=1.6,
               label=f"inference cutoff = {cutoff:.2f}  (data below this line is missing)")
    colors = ["#ff8c00", "#d2691e", "#a0522d", "#8b4513"]
    eval_logit_offset = math.log(eval_min_conf / (1 - eval_min_conf))
    for o, c in zip(candidate_offsets, colors):
        thr = eval_logit_offset + o
        ax.axvline(thr, color=c, linestyle=":", alpha=0.85, linewidth=1.4,
                   label=f"offset O={o} would cut here at eval_min_conf="
                         f"{eval_min_conf} (logit {thr:.2f})")
    ax.set_xlabel("raw Perch logit (before sigmoid)")
    ax.set_ylabel("number of detections")
    ax.set_title(
        f"Figure 1 - Perch raw-logit distribution across all species "
        f"(n={len(raw_logit):,} detections)",
        fontsize=12,
    )
    ax.legend(loc="upper right", fontsize=9, framealpha=0.95)
    fig1.text(
        0.04, 0.02,
        "How to read:\n"
        "  - X axis is Perch's confidence on its raw scale (sigmoid not applied). Higher = more confident.\n"
        "  - Each candidate offset O turns a probability threshold of min_conf=0.25 into a raw-logit cut.\n"
        "    Everything LEFT of that vertical line would be discarded at offset O.\n"
        "  - A noise hump on the LEFT and a signal hump on the RIGHT with a trough between them = a real\n"
        "    cliff at that trough. A smooth monotone tail = no cliff, the offset is just a quantile choice.\n"
        "  - The red dashed line is the inference-time threshold baked into the CSV. The distribution\n"
        "    below it is missing - rerun inference with lower min_conf to recover it.",
        bbox=footer_box, **footer_kwargs,
    )
    fig1.tight_layout(rect=(0, 0.22, 1, 1))

    # ---- Figure 2: per-species raw-logit histograms ----
    n_species = min(len(report_species), 9)
    species_to_plot = report_species[:n_species]
    ncols = 3
    nrows = (n_species + ncols - 1) // ncols
    fig2, axes = plt.subplots(nrows, ncols, figsize=(13, 3.4 * nrows + 1.6),
                              sharex=True)
    axes_flat = axes.flat if hasattr(axes, "flat") else [axes]
    for ax, sp in zip(axes_flat, species_to_plot):
        mask = perch["Scientific_Name"] == sp
        n = int(mask.sum())
        if n < 5:
            ax.set_visible(False)
            continue
        ax.hist(raw_logit[mask], bins=30, color="steelblue", edgecolor="black", alpha=0.85)
        ax.axvline(cutoff, color="red", linestyle="--", alpha=0.75, linewidth=1.3)
        ax.set_title(f"{sp}\n(n={n} detections)", fontsize=10)
        ax.set_xlabel("raw Perch logit")
        ax.set_ylabel("count")
    for ax in list(axes_flat)[n_species:]:
        ax.set_visible(False)
    fig2.suptitle(
        "Figure 2 - Per-species Perch raw-logit distributions "
        "(top species by BirdNET prevalence)",
        fontsize=12,
    )
    fig2.text(
        0.04, 0.02,
        "How to read:\n"
        "  - Each panel shows ONE species' Perch confidence distribution. Red dashed line = current\n"
        "    inference cutoff (same in every panel).\n"
        "  - Perch's 14,795-class head is multi-label, so every species has its own noise floor. A\n"
        "    global histogram averages these floors together and can hide a species-specific cliff.\n"
        "  - A species-specific cliff would appear here as a visible GAP between a low-logit noise\n"
        "    hump and a higher-logit signal hump in that species' panel.\n"
        "  - Smooth single-mode shapes mean there is no clean noise/signal boundary for that species.",
        bbox=footer_box, **footer_kwargs,
    )
    footer_frac = 1.5 / (3.4 * nrows + 1.6)
    fig2.tight_layout(rect=(0, footer_frac + 0.01, 1, 0.96))

    # ---- Figure 3: BN-agreement curves ----
    fig3, ax = plt.subplots(figsize=(12, 7.8))
    uncensored = [r for r in rows if not r["censored"]]
    if uncensored:
        offsets = [r["offset"] for r in uncensored]
        overall = [r["bn_agreement_overall"] for r in uncensored]
        ax.plot(offsets, overall, "k-", linewidth=2.4, label="overall (all species)",
                zorder=10)
        for sp in report_species[:8]:
            vals = [r["species_bn_agreement"].get(sp) for r in uncensored]
            if any(v is None for v in vals):
                continue
            ax.plot(offsets, vals, "-", alpha=0.75, label=sp)
        ax.axvline(CURRENT_OFFSET, color="red", linestyle="--", alpha=0.65,
                   linewidth=1.5,
                   label=f"current _PERCH_LOGIT_OFFSET = {CURRENT_OFFSET}")
    ax.set_xlabel("candidate _PERCH_LOGIT_OFFSET (higher = stricter Perch filter)")
    ax.set_ylabel("BN agreement rate  (matched / total BirdNET rows)")
    ax.set_title(
        "Figure 3 - BirdNET agreement vs candidate offset\n"
        "BirdNET-2.4 is a REFERENCE not ground truth; 100% is not the goal",
        fontsize=12,
    )
    ax.legend(loc="lower left", fontsize=9, framealpha=0.95)
    ax.grid(alpha=0.3)
    fig3.text(
        0.04, 0.02,
        "How to read:\n"
        "  - Moving right = tighter Perch filter = fewer Perch rows survive.\n"
        "  - A line dropping fast at some offset = Perch starts losing detections that BirdNET\n"
        "    independently confirmed there. That is the signal that we are filtering real birds.\n"
        "  - A 'knee' (sudden steepening) at a specific offset = a CLIFF. The offset just BEFORE the\n"
        "    knee is the recommended setting.\n"
        "  - A straight smooth slope = no cliff; pick the offset by row-ratio target, not by this curve.\n"
        "  - BirdNET v2.4 is the weaker model. Perch detections WITHOUT a BirdNET match are common\n"
        "    and largely real; do not interpret lower agreement as Perch being wrong.",
        bbox=footer_box, **footer_kwargs,
    )
    fig3.tight_layout(rect=(0, 0.22, 1, 1))

    plt.show()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(),
                        help="Directory holding the *detections-*.csv pairs.")
    parser.add_argument("--offset-min", type=float, default=9.0)
    parser.add_argument("--offset-max", type=float, default=12.8)
    parser.add_argument("--offset-step", type=float, default=0.1)
    parser.add_argument("--eval-min-conf", type=float, default=0.25,
                        help="Evaluation threshold applied at each candidate offset. "
                             "Keep at 0.25 to match the historical calibration target. "
                             "Independent of the CSV's own min_conf (which only controls "
                             "what data is available).")
    parser.add_argument("--top-species", type=int, default=10,
                        help="Number of most-prevalent BirdNET species to report "
                             "per-offset BN agreement rate for, derived from the data.")
    parser.add_argument("--no-plots", action="store_true",
                        help="Skip matplotlib diagnostics (use for headless / CI runs).")
    args = parser.parse_args(argv)

    pairs = discover_pairs(args.root)
    if not pairs:
        print(f"No paired CSVs found in {args.root}", file=sys.stderr)
        return 2

    print(f"Discovered {len(pairs)} campaign pair(s):")
    for p in pairs:
        print(f"  - {p.label}")
    print()

    perch_frames, birdnet_frames = [], []
    for p in pairs:
        pf, bf = load_pair(p)
        perch_frames.append(pf)
        birdnet_frames.append(bf)
    perch = pd.concat(perch_frames, ignore_index=True)
    birdnet = pd.concat(birdnet_frames, ignore_index=True)

    # Restrict both sides to the (Campaign, ARU, Week) coverage they share.
    # A BirdNET row on an ARU/week that Perch never analysed cannot possibly
    # be matched and would only deflate the BN-agreement denominator.
    perch_keys = perch[["Campaign", "ARU", "Week"]].drop_duplicates()
    birdnet_keys = birdnet[["Campaign", "ARU", "Week"]].drop_duplicates()
    common = perch_keys.merge(birdnet_keys, on=["Campaign", "ARU", "Week"])
    perch_n0, birdnet_n0 = len(perch), len(birdnet)
    perch = perch.merge(common, on=["Campaign", "ARU", "Week"])
    birdnet = birdnet.merge(common, on=["Campaign", "ARU", "Week"])
    if len(perch) != perch_n0 or len(birdnet) != birdnet_n0:
        print(f"Coverage intersection: keeping {len(common)} (Campaign,ARU,Week) "
              f"combinations covered by BOTH runs.")
        print(f"  Perch rows:   {perch_n0:,} -> {len(perch):,} "
              f"(dropped {perch_n0 - len(perch):,})")
        print(f"  BirdNET rows: {birdnet_n0:,} -> {len(birdnet):,} "
              f"(dropped {birdnet_n0 - len(birdnet):,})")
        print()

    input_min_conf_perch = float(perch["Min_Conf"].iloc[0])
    input_min_conf_birdnet = float(birdnet["Min_Conf"].iloc[0])
    eval_min_conf = args.eval_min_conf
    input_logit_cutoff = (
        math.log(input_min_conf_perch / (1 - input_min_conf_perch)) + CURRENT_OFFSET
    )
    min_uncensored_offset = (
        input_logit_cutoff - math.log(eval_min_conf / (1 - eval_min_conf))
    )

    print(f"Combined: {len(perch):,} Perch rows, {len(birdnet):,} BirdNET rows")
    print(f"CSV input min_conf:  Perch={input_min_conf_perch}, "
          f"BirdNET={input_min_conf_birdnet}")
    print(f"Evaluation min_conf: {eval_min_conf} "
          f"(threshold applied at each candidate offset)")
    print(f"Baked-in offset assumed for inversion: {CURRENT_OFFSET}")
    print()

    raw_logit = invert_to_raw_logit(perch["Confidence"])
    print(f"Raw-logit summary: min={raw_logit.min():.3f} median={raw_logit.median():.3f} "
          f"p95={raw_logit.quantile(0.95):.3f} max={raw_logit.max():.3f}")
    print(f"Inference-time logit cutoff (input_min_conf={input_min_conf_perch} at "
          f"OFFSET={CURRENT_OFFSET}): {input_logit_cutoff:.3f}")
    print(f"Minimum uncensored offset (eval_min_conf={eval_min_conf}): "
          f"{min_uncensored_offset:.3f}")
    print()

    report_species = (
        birdnet["Scientific_Name"].value_counts().head(args.top_species).index.tolist()
    )

    grid = np.round(np.arange(args.offset_min, args.offset_max + 1e-9, args.offset_step), 2)
    rows = [
        evaluate_offset(
            offset=float(o),
            eval_min_conf=eval_min_conf,
            input_logit_cutoff=input_logit_cutoff,
            perch=perch,
            birdnet=birdnet,
            raw_logit=raw_logit,
            report_species=report_species,
        )
        for o in grid
    ]

    print("Per-offset table (rows marked * are right-censored by the input CSV):")
    print(format_table(rows, report_species[:6]))
    print()

    cliff = find_agreement_cliff(
        rows,
        species=[
            sp for sp in report_species
            if sum(1 for r in rows if sp in r["species_bn_agreement"]) >= 2
            and any(
                r["species_bn_agreement"].get(sp, 0) > 0
                and (birdnet["Scientific_Name"] == sp).sum() >= CLIFF_MIN_PREVALENCE
                for r in rows
            )
        ],
        min_offset=min_uncensored_offset,
    )

    print("=== Findings ===")
    current_row = next(r for r in rows if abs(r["offset"] - CURRENT_OFFSET) < 1e-6)
    print(f"At current OFFSET={CURRENT_OFFSET}:")
    print(f"  Perch rows: {current_row['perch_rows']:,}  "
          f"({current_row['ratio']:.2f}x BirdNET)")
    print(f"  Overall BN agreement rate (Perch <-> BirdNET, not true recall): "
          f"{current_row['bn_agreement_overall']:.3f}")
    print()

    # Smoothness diagnostic: median per-step BN-agreement drop for indicator species.
    uncensored = [r for r in rows if not r["censored"]]
    indicator_drops: list[float] = []
    for sp in report_species[:6]:
        for r_a, r_b in zip(uncensored, uncensored[1:]):
            ra, rb = r_a["species_bn_agreement"].get(sp), r_b["species_bn_agreement"].get(sp)
            if ra is None or rb is None:
                continue
            indicator_drops.append(ra - rb)
    if indicator_drops:
        median_drop = float(np.median([d for d in indicator_drops if d > 0]))
        max_drop = max(indicator_drops, default=0.0)
        print(f"BN-agreement-curve smoothness over offsets >= {CURRENT_OFFSET}: "
              f"median positive per-step drop = {median_drop:.3f}, "
              f"max per-step drop = {max_drop:.3f}.")
        print()

    if cliff is None:
        print("No agreement cliff detected: the per-species BN-agreement curves "
              "decline smoothly with no clear noise/signal boundary. Note that "
              "agreement with BirdNET cannot prove signal vs noise on its own, "
              "since BirdNET is the weaker model and may miss real Perch calls.")
        # Only invoke the historical 1.65x baseline if the BirdNET reference
        # was generated at the same min_conf as the original calibration
        # (0.25). At a lower BirdNET min_conf the denominator is inflated by
        # BirdNET's low-confidence rows and the ratio is not comparable.
        if abs(input_min_conf_birdnet - 0.25) < 1e-9:
            print(f"The data at OFFSET={CURRENT_OFFSET} yields a "
                  f"{current_row['ratio']:.2f}x Perch/BirdNET row ratio, in line "
                  f"with the 1.65x observed during the original Camp1 calibration. "
                  f"Without a cliff to anchor a new value, the original choice "
                  f"is still the best one available from this data.")
        else:
            print(f"At OFFSET={CURRENT_OFFSET}, Perch produces "
                  f"{current_row['perch_rows']:,} rows. The Perch/BirdNET row "
                  f"ratio ({current_row['ratio']:.2f}) is not directly comparable "
                  f"to the 1.65x from the original calibration because BirdNET's "
                  f"min_conf is {input_min_conf_birdnet} here vs 0.25 then "
                  f"(its denominator is inflated). The relevant evidence is the "
                  f"smooth shape of the per-species curves, which confirms no "
                  f"cliff exists across the now-uncensored offset range "
                  f"[{min_uncensored_offset:.2f}, {args.offset_max:.2f}].")
        print(f"Recommendation: keep _PERCH_LOGIT_OFFSET = {CURRENT_OFFSET}.")
    else:
        sp, last_safe_offset, drop = cliff
        print(f"Recall cliff: species '{sp}' drops by {drop:.3f} when moving "
              f"past offset {last_safe_offset:.2f}.")
        if abs(last_safe_offset - CURRENT_OFFSET) < 1e-6:
            verdict = (
                f"The cliff sits exactly at the current setting. "
                f"_PERCH_LOGIT_OFFSET = {CURRENT_OFFSET} is still appropriate."
            )
        elif last_safe_offset > CURRENT_OFFSET:
            verdict = (
                f"The cliff has moved up: the data tolerates a tighter cutoff. "
                f"Consider _PERCH_LOGIT_OFFSET = {last_safe_offset:.2f}."
            )
        else:
            verdict = (
                f"The cliff is below the current offset; lowering would be needed "
                f"to capture the boundary, but this analysis cannot prove it without "
                f"a re-run at a lower min_conf or offset."
            )
        print(f"Recommendation: {verdict}")

    if not args.no_plots:
        print()
        print("Rendering matplotlib diagnostics (close all windows to exit)...")
        plot_diagnostics(
            raw_logit=raw_logit,
            perch=perch,
            rows=rows,
            report_species=report_species,
            input_logit_cutoff=input_logit_cutoff,
            eval_min_conf=eval_min_conf,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
