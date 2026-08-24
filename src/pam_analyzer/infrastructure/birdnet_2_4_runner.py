"""BirdNET v2.4 infrastructure adapter.

Birdnet24Runner is the AnalysisRunner backed by BirdNET v2.4, kept
alongside the v3.0 runner so a monitoring study that started under v2.4 can
keep extending its time series under the same model, and so there is a
non-preview engine available while v3.0 ships as a preview build.

Upstream publishes no ONNX export for v2.4, so the weights this loads are
converted from upstream's SavedModel by scripts/convert_birdnet_2_4_onnx.py
and loaded through birdnet_2_4_onnx.

v2.4 labels its classes on the older eBird-based axis. The species filter
matches on that axis (the allow-list comes from the geo model of the same
generation, via TAXONOMY_V2_4), but the name written to CSV is rewritten to
the project's chosen axis by legacy_names.to_axis(), so both engines' rows
line up in the Examine grid. On the default v3.0 axis that turns Accipiter
gentilis into Astur gentilis. On the v2.4 axis it is a no-op, which is what
a study with years of v2.4 CSVs behind it wants. The Model column records
which engine actually produced each row.

The lib's `species_name` in result rows is in 'Scientific_Common' format
because we load the model with lang='en_us'. We split each entry to get the
scientific name and the English common name. Other locales come from the
taxonomy's locale_label_map().
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, ClassVar

from ..domain import AnalysisSettings
from .base_analysis_runner import BaseAnalysisRunner, ParsedRow
from .birdnet_lib import TAXONOMY_V2_4
from .legacy_names import to_axis

MODEL_KEY = "BirdNET-2.4"


def _split_sci_common(species_name: str) -> tuple[str, str]:
    """Split a 'Scientific_Common' label entry into (sci, common)."""
    sci, _, common = species_name.partition("_")
    return sci, common


class Birdnet24Runner(BaseAnalysisRunner):
    """AnalysisRunner implementation backed by BirdNET v2.4 (ONNX).

    Loads the model once per run and reuses it across campaigns. The lib
    handles audio I/O, 3 s window framing, batched inference, sigmoid
    scoring, and the confidence threshold. Writes
    <campaign>/detections-BirdNET-2.4.csv plus the per-week species-list
    TXT files via the base class.
    """

    model_key: ClassVar[str] = MODEL_KEY
    log_prefix: ClassVar[str] = "birdnet2.4"
    taxonomy = TAXONOMY_V2_4

    def _load_model(self) -> Any:
        """Load BirdNET v2.4's converted weights on onnxruntime.

        birdnet.load cannot express this: the lib has no ONNX backend for
        v2.4, so birdnet_2_4_onnx pairs one it defines with the lib's own
        AcousticModelV2_4, which supplies the 48 kHz, 144000-sample
        preprocessing contract.

        Loaded with en_us so result rows carry English common names in the
        'Sci_Common' species_name string.
        """
        from .birdnet_2_4_onnx import load_acoustic

        return load_acoustic("en_us")

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
        session-bound and cannot change between weeks, so a single session
        plus row-level checks yields the same filtered output as one session
        per week without the per-week model warmup.

        Unlike v3.0, v2.4 emits raw logits, so apply_sigmoid=True is a real
        squashing step rather than a pass-through. sigmoid_sensitivity=1.0 is
        the neutral setting and keeps confidences comparable with what
        earlier releases of this app wrote. top_k=None (the lib defaults to
        5) returns every class above the threshold, matching v3.0's
        behaviour so the two engines' row counts stay comparable.
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
        """Convert one raw lib result row into a ParsedRow.

        Common-name lookups key on the v2.4 spelling because the label maps
        come from v2.4's own label files. Only the written scientific name
        moves to the project's axis.
        """
        sci, common_en = _split_sci_common(str(raw_row["species_name"]))
        out_name = to_axis(sci, settings.canonical_taxonomy)
        preferred = preferred_lang_map.get(sci) or common_en or out_name

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
            scientific_name=out_name,
            match_name=sci,
            confidence=float(raw_row["confidence"]),
            preferred_common=preferred,
            locale_commons=locale_commons,
        )
