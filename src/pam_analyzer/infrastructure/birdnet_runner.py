"""BirdNET v2.4 infrastructure adapter.

BirdnetRunner is an AnalysisRunner backed by BirdNET v2.4 (TFLite), loaded
via the birdnet>=0.2 library. Audio I/O, 3 s window framing, batched
inference, sigmoid scoring, and the confidence threshold all live inside
the lib's predict_session pipeline. The per-campaign loop, species-filter
resolution, ARU/rank computation, and CSV writing live in
BaseAnalysisRunner; this file only contributes the three per-model hooks
(`_load_model`, `_open_predict_session`, `_parse_row`).

The lib's `species_name` in result rows is in 'Scientific_Common' format
because we load the model with lang='en_us'. We split each entry to get
the scientific name (canonical axis for the allow-list check) and the
English common name; other locales come from locale_label_map().
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from ..domain import AnalysisSettings
from .base_analysis_runner import BaseAnalysisRunner, ParsedRow


def _split_sci_common(species_name: str) -> tuple[str, str]:
    """Split a 'Scientific_Common' label entry into (sci, common)."""
    sci, _, common = species_name.partition("_")
    return sci, common


class BirdnetRunner(BaseAnalysisRunner):
    """AnalysisRunner implementation backed by BirdNET v2.4 (TFLite).

    Loads the model once via birdnet.load('acoustic', '2.4', 'tf') and
    reuses it across campaigns and files. The lib handles 48 kHz audio
    I/O, 3 s window framing, batched TFLite inference, sigmoid scoring,
    and the confidence threshold. Writes
    <campaign>/<campaign>-detections-BirdNET-2.4.csv plus the per-week
    species-list TXT files via the base class.
    """

    model_key = "BirdNET-2.4"
    log_prefix = "birdnet"

    def _load_model(self) -> Any:
        import birdnet

        # Load with en_us so result rows carry English common names in the
        # 'Sci_Common' species_name string. Other locales come from
        # locale_label_map() lookups inside _parse_row.
        return birdnet.load("acoustic", "2.4", "tf", lang="en_us")

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
        # mask is session-bound and cannot change between weeks, so a single
        # global session plus row-level checks yields the same filtered
        # output as one-session-per-week without the per-week model warmup.
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
        species_name = str(raw_row["species_name"])
        sci, common_en = _split_sci_common(species_name)

        # Preferred-language common name. Fall back to the lib's en_us
        # common name if the locale lookup misses (e.g. a recently added
        # species not yet translated in the user's lang).
        preferred = preferred_lang_map.get(sci, common_en or sci)

        # For the en_us column we reuse the lib-provided common name
        # directly to avoid a redundant locale_map lookup that would return
        # the same string.
        locale_commons = {
            loc: (
                common_en
                if loc == "en_us"
                else locale_maps[loc].get(sci, "")
            )
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
