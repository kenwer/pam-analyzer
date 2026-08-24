"""BirdNET v3.0 infrastructure adapter.

BirdnetRunner is the AnalysisRunner implementation, backed by BirdNET v3.0
on the ONNX backend via the birdnet>=1.1 library. Audio I/O, 3 s window
framing, batched inference, sigmoid scoring, and the confidence threshold
all live inside the lib's predict_session pipeline. Per-campaign
sequencing, progress reporting, species filtering, and CSV writing come
from BaseAnalysisRunner. This module supplies only the three model-specific
hooks.

The lib's `species_name` in result rows is in 'Scientific_Common' format
because we load the model with lang='en_us'. We split each entry to get the
scientific name (the axis for the allow-list check) and the English common
name. Other locales come from locale_label_map().
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, ClassVar

from ..domain import AnalysisSettings
from .base_analysis_runner import BaseAnalysisRunner, ParsedRow
from .birdnet_lib import MODEL_PRECISION

# Hardcoded rather than derived from the library because this string is part of
# the CSV filename on the user's disk. The version suffix has to name the release
# the library ships, which test_model_identity.py checks.
MODEL_KEY = "BirdNET-3.0-preview3.1"


def _split_sci_common(species_name: str) -> tuple[str, str]:
    """Split a 'Scientific_Common' label entry into (sci, common)."""
    sci, _, common = species_name.partition("_")
    return sci, common


class BirdnetRunner(BaseAnalysisRunner):
    """AnalysisRunner implementation backed by BirdNET v3.0 (ONNX).

    Loads the model once per run and reuses it across campaigns. Writes
    <campaign>/detections-<MODEL_KEY>.csv plus the per-week species-list
    TXT files.
    """

    model_key: ClassVar[str] = MODEL_KEY
    log_prefix: ClassVar[str] = "birdnet"

    def _load_model(self) -> Any:
        """Load BirdNET v3.0 on the ONNX backend.

        Loaded with en_us so result rows carry English common names in the
        'Sci_Common' species_name string. Other locales come from
        locale_label_map() lookups inside _parse_row.
        """
        import birdnet

        return birdnet.load("acoustic", "3.0", "onnx", lang="en_us", precision=MODEL_PRECISION)

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

        v3.0 bakes the sigmoid into the ONNX graph, so apply_sigmoid=True is
        the lib's documented way of saying 'return those probabilities
        unchanged' rather than a request for a second squashing. For the same
        reason sigmoid_sensitivity must stay 1.0 and apply_softmax is rejected
        outright. The raw logits a softmax would need are not exported.
        """
        return model.predict_session(
            default_confidence_threshold=settings.min_conf,
            custom_species_list=None,
            overlap_duration_s=settings.overlap,
            top_k=None,
            apply_sigmoid=True,
            sigmoid_sensitivity=1.0,
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
        """Convert one raw lib result row into a ParsedRow."""
        sci, common_en = _split_sci_common(str(raw_row["species_name"]))

        # Fall back to the lib's en_us common name if the locale lookup misses
        # (e.g. a species not yet translated in the user's language). `or`
        # rather than a .get default because locale_label_map keeps entries
        # whose common name is blank, and a blank translation has to degrade
        # the same way a missing key does.
        preferred = preferred_lang_map.get(sci) or common_en or sci

        # For the en_us column reuse the lib-provided common name directly,
        # avoiding a locale_map lookup that would return the same string.
        locale_commons = {
            loc: (common_en if loc == "en_us" else locale_maps[loc].get(sci, ""))
            for loc in settings.locales
        }

        return ParsedRow(
            file_path=Path(str(raw_row["input"])),
            start_time=float(raw_row["start_time"]),
            end_time=float(raw_row["end_time"]),
            scientific_name=sci,
            confidence=float(raw_row["confidence"]),
            preferred_common=preferred,
            locale_commons=locale_commons,
        )
