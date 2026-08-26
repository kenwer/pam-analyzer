"""Perch v2 infrastructure adapter.

PerchRunner is an AnalysisRunner backed by Google's Perch v2, running on
onnxruntime through the backend defined in perch_onnx. Audio I/O, resampling to
32 kHz, 5 s window framing and batched inference all live inside the birdnet
lib's predict_session pipeline. Per-campaign sequencing, species filtering and
CSV writing come from BaseAnalysisRunner. This module supplies the three
model-specific hooks plus the calibration that makes Perch's scores comparable
with the BirdNET runners' output.

Two things set this runner apart from the BirdNET ones.

Perch emits raw class logits, not probabilities, and they do not sit around
zero the way BirdNET v2.4's do. Its head is positive everywhere: silence alone
lands near +4.5 and real ambient noise higher still, so a plain sigmoid would
report every window's top classes at ~0.99. _PERCH_2_0_LOGIT_OFFSET shifts the
curve so that Confidence means what it means for the other engines.

Perch labels its classes with bare names, where BirdNET uses
'Scientific_Common'. Its label set also reaches past birds into insects,
amphibians and FSD50k sound events, some of which carry underscores of their
own. A row parser that split on the first underscore, as the BirdNET runners
must, would silently truncate those, so model output is never split here.

A line of a user's species list is a different matter, because it may have
been written for either engine. The base class reads one against the axis
below: a line that already names a Perch class stays whole, and anything else
is read the BirdNET way, so a hand-written list survives a switch to this
engine.

Perch's species axis converges with BirdNET v3.0's: 10916 of v3.0's 11560
classes are spelled identically in Perch's 14795. That is why this runner can
reuse TAXONOMY_V3_0 for the geo allow-list and the common-name maps rather than
needing a Perch-specific crosswalk. The classes Perch adds and v3.0 lacks are
mostly non-birds, and a per-week allow-list drops them the same way it drops
any name absent from the axis.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, ClassVar

from ..domain import AnalysisSettings
from .base_analysis_runner import BaseAnalysisRunner, ParsedRow
from .birdnet_lib import TAXONOMY_V3_0
from .species_names import canonical

# Hardcoded rather than derived from the model, because this string is part of
# the CSV filename on the user's disk.
MODEL_KEY = "Perch-2.0"

# Perch resolves to roughly 1.25 GB resident per inference worker: onnxruntime
# prepacks its 363 MB classifier weight, which is 89% of the model, into a
# second copy. One worker per core would need about 18.9 GB, more than BirdNET
# v3.0's 11.5 GB. Half the workers with two threads each measured 96.5 seg/s
# against 98.0, so 1.5% of throughput buys half the memory.
#
# The BirdNET runners set this independently, and v3.0 lands the other way for
# a different reason. Whichever way it goes, SESSION_THREADS and the n_workers
# below are one decision and have to move together.
SESSION_THREADS = 2

# Perch v2's class head emits positive logits everywhere, so thresholding a
# sigmoid of them at face value would keep almost every window.
#
# The offset was calibrated by cross-comparison against BirdNET v2.4 on the
# Camp1 campaign at min_conf=0.2, then checked against spot-verified borderline
# detections. 11.2 rather than a rounder number because per-species recall
# against BirdNET shows a cliff between 11.2 and 11.3: Corvus corone holds at
# 100% up to 11.2, then falls to 87% at 11.3 and 69% at 11.5. That cliff is the
# boundary between genuine quiet calls and noise.
_PERCH_2_0_LOGIT_OFFSET = 11.2


def _n_workers() -> int:
    """Enough workers for SESSION_THREADS each to fill the physical cores.

    Physical rather than logical cores, matching what the lib's own
    `n_workers=None` resolves to, so every engine sizes the same machine the
    same way.
    """
    import psutil

    cores = psutil.cpu_count(logical=False) or 1
    return max(1, cores // SESSION_THREADS)


def _perch_logit_threshold(min_conf: float) -> float:
    """Map a probability-space threshold into Perch's logit space.

    Clamped away from 0 and 1 because the log would diverge there, and a
    min_conf of exactly 0 or 1 is reachable from the settings UI.
    """
    p = min(max(min_conf, 1e-6), 1.0 - 1e-6)
    return math.log(p / (1.0 - p)) + _PERCH_2_0_LOGIT_OFFSET


def _perch_logit_to_prob(logit: float) -> float:
    """Calibrated probability for a Perch v2 logit, the inverse of the threshold map."""
    return 1.0 / (1.0 + math.exp(-(logit - _PERCH_2_0_LOGIT_OFFSET)))


class PerchRunner(BaseAnalysisRunner):
    """AnalysisRunner implementation backed by Perch v2 (ONNX).

    Loads the model once per run and reuses it across campaigns. Writes
    <campaign>/detections-Perch-2.0.csv plus the per-week species-list TXT
    files, so a Perch run and a BirdNET run can coexist on one campaign.
    """

    model_key: ClassVar[str] = MODEL_KEY
    log_prefix: ClassVar[str] = "perch"
    taxonomy = TAXONOMY_V3_0

    def _model_classes(self) -> AbstractSet[str]:
        """Perch's own classes.

        Thousands of them are insects, amphibians, mammals and FSD50k sound
        events that BirdNET-3.0, bound above as `taxonomy`, has no name for.
        """
        # lazy load to prevent perch_onnx to pull in the birdnet lib at app startup via _load_model
        from .perch_onnx import label_set

        return label_set()

    def _load_model(self) -> Any:
        """Load Perch v2 on onnxruntime.

        birdnet.load_perch_v2() cannot express this: the lib declares Perch
        pb-only and would return the TensorFlow SavedModel, which the app has
        no TensorFlow to run. perch_onnx pairs a locally-defined ONNX backend
        with the lib's own AcousticModelPerchV2, which supplies the 32 kHz,
        160000-sample preprocessing contract.

        No lang argument, unlike the BirdNET runners: Perch ships one label set
        of bare scientific names and no locales of its own.
        """
        from .perch_onnx import load_acoustic

        return load_acoustic(threads=SESSION_THREADS)

    def _open_predict_session(
        self,
        model: Any,
        *,
        settings: AnalysisSettings,
        files_total: int,
        on_stats: Callable[[Any], None],
    ) -> AbstractContextManager[Any]:
        """Open the inference session for one campaign.

        custom_species_list is intentionally None: the per-week allow-list is
        applied as a post-filter on result rows instead. The lib's mask is
        session-bound and cannot change between weeks, so a single session plus
        row-level checks yields the same filtered output as one session per
        week without the per-week model warmup.

        apply_sigmoid=False because the calibration this model needs is not a
        plain sigmoid. The threshold is translated into logit space here and
        the score is translated back in _parse_row, so the CSV still carries a
        0-1 Confidence.

        top_k caps per-segment emissions and is 50 on all three runners. The
        cap exists for memory, not row count: the lib's result tensor is dense
        over top_k, shaped (n_files, n_segments, top_k) at 7 bytes per slot,
        and the app hands it a whole campaign at once. None resolves to
        n_species, which on a 6893-file campaign of 2-minute recordings is
        17.1 GB for Perch's 14795 classes, against 58 MB at 50.

        The cap is applied before the per-week allow-list, which runs as a
        post-filter in BaseAnalysisRunner, so a cap low enough to fill with
        non-birds can drop an in-region species that ranked below them.

        Perch alone would hold at 25. Against uncapped runs over a 243-file
        February campaign and a 350-file dawn-chorus sample, in-region recall
        at min_conf 0.01 is 54% and 44% at top_k=5, 99.9% at 25 and 100% at 50,
        and from min_conf 0.02 upward 25 already loses nothing. At the 0.25
        default the cap never binds at all, because at most 4 classes clear the
        calibrated threshold in any one segment. The size is set by the BirdNET
        runners, whose sigmoid scores put several times more classes over a low
        threshold than this model's calibrated logit does.

        n_workers is set rather than left at the lib's default of one worker
        per core, because each worker's session runs SESSION_THREADS threads.
        """
        return model.predict_session(
            default_confidence_threshold=_perch_logit_threshold(settings.min_conf),
            custom_species_list=None,
            overlap_duration_s=settings.overlap,
            top_k=50,
            apply_sigmoid=False,
            n_producers=1,
            n_workers=_n_workers(),
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
        """Convert one raw lib result row into a ParsedRow.

        The species name is taken whole. Perch labels are bare names, and the
        non-bird half of its label set contains underscores that a
        'Scientific_Common' split would truncate.

        Common-name lookups key on the canonicalised name, since that is what
        the label maps (v3.0's files) are keyed on too.
        """
        sci = str(raw_row["species_name"])
        # The lib returned a raw logit because the session set
        # apply_sigmoid=False. Converting here keeps the CSV's Confidence in
        # the same units the BirdNET runners write.
        conf = _perch_logit_to_prob(float(raw_row["confidence"]))

        name = canonical(sci)

        # `or` rather than a .get default, because locale_label_map keeps
        # entries whose common name is blank and a blank translation has to
        # degrade the same way a missing key does. Perch classes outside
        # v3.0's axis, the insects and sound events, fall through to the
        # scientific name.
        preferred = preferred_lang_map.get(name) or name
        locale_commons = {loc: locale_maps[loc].get(name, "") for loc in settings.locales}

        return ParsedRow(
            file_path=Path(str(raw_row["input"])),
            start_time=float(raw_row["start_time"]),
            end_time=float(raw_row["end_time"]),
            scientific_name=name,
            confidence=conf,
            preferred_common=preferred,
            locale_commons=locale_commons,
        )
