"""BirdNET v2.4 infrastructure adapter.

Birdnet24Runner is the AnalysisRunner backed by BirdNET v2.4, kept
alongside the v3.0 runner so a monitoring study that started under v2.4 can
keep extending its time series under the same model, and so there is a
non-preview engine available while v3.0 ships as a preview build.

Upstream publishes no ONNX export for v2.4, so the package downloads
upstream's Keras weights and converts them on this machine
(birdnet_2_4_convert), and the result loads through birdnet_2_4_onnx.

v2.4 labels its classes on the older eBird-based axis. Every name is
canonicalised at the boundary (species_names.canonical), so the species
filter and the name written to CSV both speak the app's one namespace, and
both engines' rows line up in the Examine grid. That turns Accipiter
gentilis into Astur gentilis on the way in. The Model column records which
engine actually produced each row.

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

from ..domain import AnalysisSettings, ModelInfo
from . import birdnet_2_4_convert
from ._analysis_helpers import workers_for
from .base_analysis_runner import BaseAnalysisRunner, ParsedRow
from .birdnet_lib import TAXONOMY_V2_4
from .model_store import ModelPackage
from .species_names import canonical

MODEL_KEY = "BirdNET-2.4"

# v2.4 runs fastest on half as many workers as the machine has cores, two
# onnxruntime threads each. Over 243 one-minute files on 14 cores that measured
# 334-362 seg/s at 4.3 GiB against 293-310 seg/s at 8.8 GiB for the lib's
# default of one single-threaded worker per core. v3.0 lands the other way and
# keeps the default.
THREADS_PER_WORKER = 2


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
    package: ClassVar[ModelPackage] = ModelPackage(
        info=ModelInfo(
            name="BirdNET v2.4",
            info_url="https://zenodo.org/records/15050749",
            licence="CC BY-NC-SA 4.0",
        ),
        artifacts=birdnet_2_4_convert.artifacts,
        prepare=birdnet_2_4_convert.prepare,
    )

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
        earlier releases of this app wrote.

        top_k matches v3.0's cap so the two engines' row counts stay
        comparable, and is set for memory rather than for row count. The lib's
        result tensor is dense over top_k, shaped (n_files, n_segments, top_k)
        at 7 bytes per slot, and the app hands it a whole campaign at once.
        None resolves to n_species, which on a 6893-file campaign of 2-minute
        recordings is 12.6 GB for v2.4's 6522 classes, against 97 MB at 50.

        Measured the same way as v3.0, over uncapped runs on a 243-file
        February campaign and a 350-file dawn-chorus sample. v2.4 is sparser
        than v3.0 above a low threshold and holds 100% in-region recall from
        k=5 up at the 0.10 floor the min confidence slider allows, where at
        most 7 classes clear the threshold in any one segment. It would hold at
        25, and stays at 50 to keep one cap across all three runners.

        n_workers is set rather than left at the lib's default of one worker
        per core, so that each worker's session gets THREADS_PER_WORKER threads.
        """
        return model.predict_session(
            default_confidence_threshold=settings.min_conf,
            custom_species_list=None,
            overlap_duration_s=settings.overlap,
            top_k=50,
            apply_sigmoid=True,
            sigmoid_sensitivity=1.0,
            n_producers=1,
            n_workers=workers_for(THREADS_PER_WORKER),
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

        The label is canonicalised first, so the common-name lookups key on
        the same name the row is written under.
        """
        sci, common_en = _split_sci_common(str(raw_row["species_name"]))
        # Canonicalised at the boundary, so every lookup and comparison below
        # is in the app's namespace rather than the model's.
        name = canonical(sci)

        # Fall back to the lib's en_us common name if the locale lookup misses
        # (e.g. a species not yet translated in the user's language). `or`
        # rather than a .get default because locale_label_map keeps entries
        # whose common name is blank, and a blank translation has to degrade
        # the same way a missing key does.
        preferred = preferred_lang_map.get(name) or common_en or name

        # For the en_us column reuse the lib-provided common name directly,
        # avoiding a locale_map lookup that would return the same string.
        locale_commons = {
            loc: (common_en if loc == "en_us" else locale_maps[loc].get(name, ""))
            for loc in settings.locales
        }

        return ParsedRow(
            file_path=Path(str(raw_row["input"])),
            start_time=float(raw_row["start_time"]),
            end_time=float(raw_row["end_time"]),
            scientific_name=name,
            confidence=float(raw_row["confidence"]),
            preferred_common=preferred,
            locale_commons=locale_commons,
        )
