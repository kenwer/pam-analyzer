"""Perch v2 infrastructure adapter.

PerchRunner is an AnalysisRunner backed by Google's Perch v2 SavedModel,
loaded via the birdnet>=0.2 library. Most of the heavy lifting (audio I/O,
resampling, 5 s window framing, batched inference, threshold filtering)
lives inside the lib's predict_session pipeline. The per-campaign loop,
species-filter resolution, ARU/rank computation, and CSV writing live in
BaseAnalysisRunner; this file only contributes the three per-model hooks
(`_load_model`, `_open_predict_session`, `_parse_row`) plus the logit
calibration that converts Perch's raw scores to a probability comparable
with BirdNET's sigmoid output.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from ..domain import AnalysisSettings
from .base_analysis_runner import BaseAnalysisRunner, ParsedRow
from .birdnet_lib import load_perch_v2_pinned

# Perch v2's class head emits positive logits everywhere: pure silence
# sits around +4.5, and real ambient noise (wind, distant traffic) sits
# higher still. Without an offset, every 5 s window emits its top-k
# species at ~0.99 sigmoid even when no bird is calling.
#
# The offset was tuned by cross-comparison against BirdNET v2.4 on the
# Camp1 campaign at min_conf=0.2 (843 BirdNET rows, 1547 Perch rows at
# OFFSET=11.0). Initial calibration looked at where BirdNET and Perch
# agreed on species in the same window; refined after the user spot-
# checked the borderline detections and confirmed that low-confidence
# Perch rows for at least Corvus corone are real distant/quiet calls,
# not noise.
#
# The 11.2 setting was chosen because per-species recall vs BirdNET
# shows a sharp cliff between 11.2 and 11.3: Corvus corone holds at
# 100% up to 11.2 then collapses to 87% at 11.3 and 69% at 11.5. That
# cliff marks the boundary between genuine quiet calls and noise.
# Quantitatively: 1391 Perch rows (1.65x BirdNET), 97.4% retention of
# cross-validated agreements, 10% reduction vs OFFSET=11.0's 1547 rows.
#
# BirdNET v2.4 does not need this: its logits are centred around 0.
_PERCH_LOGIT_OFFSET = 11.2


def _perch_logit_threshold(min_conf: float) -> float:
    """Map a probability-space threshold to Perch's logit space."""
    p = min(max(min_conf, 1e-6), 1.0 - 1e-6)
    return math.log(p / (1.0 - p)) + _PERCH_LOGIT_OFFSET


def _perch_logit_to_prob(logit: float) -> float:
    """Calibrated probability for a Perch v2 logit, inverse of the threshold map."""
    return 1.0 / (1.0 + math.exp(-(logit - _PERCH_LOGIT_OFFSET)))


class PerchRunner(BaseAnalysisRunner):
    """AnalysisRunner implementation backed by Google's Perch v2 model.

    Loads the model once via load_perch_v2_pinned() (resolved from the
    local kagglehub cache, downloaded only on a cold cache) and reuses it
    across campaigns and files. The lib handles audio I/O, resampling to 32 kHz,
    5 s window framing, and batched TF inference. We operate in raw-logit
    space (apply_sigmoid=False) and translate to a calibrated probability
    inside _parse_row; see _PERCH_LOGIT_OFFSET for why a vanilla sigmoid is
    not appropriate for Perch v2. Output goes to
    <campaign>/<campaign>-detections-Perch-2.0.csv so it can coexist with a
    parallel BirdNET run on the same campaign.

    Performance (Apple M4 Pro, CPU only, 4 h 3 min of audio, 243 WAV files):
        Perch v2:     ~3 min 25 s wall, ~77x real-time   (RTF ~0.013)
        BirdNET v2.4: ~15 s wall,     ~1050x real-time (RTF ~0.001)
    Perch is roughly 13x slower per second of audio than BirdNET. The
    gap is the cost of Perch's larger conformer-style architecture vs
    BirdNET's small CNN; on GPU the gap narrows considerably. Plan for
    roughly 50 s of wall time per 1 h of audio on this hardware, and
    set user-facing ETAs accordingly.
    """

    model_key = "Perch-2.0"
    log_prefix = "perch"

    def _load_model(self) -> Any:
        return load_perch_v2_pinned()

    def _open_predict_session(
        self,
        model: Any,
        *,
        settings: AnalysisSettings,
        files_total: int,
        on_stats: Callable[[Any], None],
    ) -> AbstractContextManager[Any]:
        # custom_species_list is intentionally None: the base class applies
        # the per-week allow-list as a post-filter on result rows. The lib's
        # mask is session-bound (cannot change between weeks), so a single
        # global session plus our row-level check gives the same filtered
        # output as one-session-per-week would, without paying the
        # SavedModel reload cost on every week boundary.
        #
        # apply_sigmoid=False follows the library's own default for Perch v2.
        # The model emits raw class logits and we threshold in logit space
        # via _perch_logit_threshold(); rows are converted back to a
        # calibrated probability inside _parse_row before being written, so
        # CSV Confidence stays in 0-1 and matches the BirdNET runner's units.
        #
        # top_k=5 caps per-segment emissions. Perch is multi-label across
        # 14,795 classes; without a top-K cap a single campaign produced
        # ~300k rows, most of them low-quality co-activations. 5 matches the
        # lib's own default and the 1-3 species per segment a well-tuned
        # acoustic model typically surfaces.
        return model.predict_session(
            default_confidence_threshold=_perch_logit_threshold(settings.min_conf),
            custom_species_list=None,
            overlap_duration_s=settings.overlap,
            top_k=5,
            apply_sigmoid=False,
            n_producers=1,
            n_workers=None,
            batch_size=8,
            show_stats="progress",
            progress_callback=on_stats,
            max_n_files=files_total,
            device="CPU",
        )

    def _parse_row(
        self,
        raw_row: Any,
        *,
        preferred_lang_map: dict[str, str],
        locale_maps: dict[str, dict[str, str]],
        settings: AnalysisSettings,
    ) -> ParsedRow:
        sci = str(raw_row["species_name"])
        # The lib returned a raw logit because apply_sigmoid=False; convert
        # here so CSV Confidence stays a 0-1 probability and matches the
        # BirdNET runner's units. See _PERCH_LOGIT_OFFSET.
        conf = _perch_logit_to_prob(float(raw_row["confidence"]))

        preferred = preferred_lang_map.get(sci, sci)
        locale_commons = {
            loc: locale_maps[loc].get(sci, "") for loc in settings.locales
        }

        return ParsedRow(
            file_path=Path(str(raw_row["input"])),
            start_time=float(raw_row["start_time"]),
            end_time=float(raw_row["end_time"]),
            scientific_name=sci,
            confidence=conf,
            preferred_common=preferred,
            locale_commons=locale_commons,
        )
