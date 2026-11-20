"""BirdNET v2.4's taxonomy: its own label axis, named in the v3.0 reference taxonomy.

The label files and geo model come from the weights birdnet_2_4_convert
builds on this machine. Common names come from birdnet_3_0_taxonomy, so one
species reads the same whichever engine found it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from . import birdnet_3_0_taxonomy
from .birdnet_taxonomy import TaxonomyServices


class _Taxonomy(TaxonomyServices):
    """BirdNET v2.4, whose common names come from a reference taxonomy.

    Its geo model lists its own sound classes, so they already reach the
    allow-list and _sound_classes stays empty.
    """

    def __init__(self, names_from: TaxonomyServices) -> None:
        super().__init__()
        self._names_from = names_from

    def available_locales(self) -> tuple[str, ...]:
        import birdnet

        return tuple(sorted(birdnet.VALID_MODEL_LANGUAGES_V2_4))

    def _label_entries(self, lang: str) -> Sequence[str]:
        """Read from the converted model's own label directory.

        Going through the lib's TFLite downloader instead would fetch 51 MB of
        weights the app no longer runs, just to reach the text files beside them.
        """
        from . import birdnet_2_4_onnx

        return birdnet_2_4_onnx.labels("acoustic", lang)

    def _load_geo_model(self) -> Any:
        """The lib ships no ONNX backend for either v2.4 model, so birdnet.load
        would raise. The locally-defined backend runs the weights converted on
        this machine instead.
        """
        from . import birdnet_2_4_onnx

        return birdnet_2_4_onnx.load_geo("en_us")

    def _label_map(self, lang: str) -> dict[str, str]:
        """Only values change: the keys stay v2.4's axis, so one species reads
        the same whichever engine found it without widening what v2.4 detects.
        """
        reference = self._names_from.locale_label_map(lang)
        return {name: reference.get(name) or common for name, common in super()._label_map(lang).items()}


TAXONOMY: TaxonomyServices = _Taxonomy(names_from=birdnet_3_0_taxonomy.TAXONOMY)
