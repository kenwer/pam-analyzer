"""ONNX backends for BirdNET v2.4, which the birdnet lib does not ship.

`birdnet.load('acoustic', '2.4', 'onnx')` raises: upstream exports v2.4 as
TFLite and SavedModel only, and the lib's loader is a hardcoded decision tree
with one arm per (version, backend) pair rather than a registry. The v2.4 arms
are 'tf' and 'pb'.

The app needs ONNX anyway. ai-edge-litert, which the 'tf' backend runs on,
publishes no Intel macOS wheel, so v2.4 could not load there at all, and it
publishes cp313 wheels only, which pins the project's Python ceiling. Running
both v2.4 models on the same onnxruntime the v3.0 engine already uses gives one
runtime on every platform.

What makes this possible is that a birdnet "backend" is the only
version-specific part. `OnnxBackend` is generic (a session, an input name, and
output lookups by index), and the model class holds the preprocessing contract:
48 kHz, 3 s, 144000 samples for v2.4, against v3.0's 32 kHz and 96000. Pairing
a locally-defined backend with the lib's own `AcousticModelV2_4` therefore
changes the inference runtime and nothing else.

`load()` is used rather than `load_custom()` because it takes `backend_type`
directly, skips the '.onnx' suffix check, and skips the `check_validity`
subprocess spawn. Nothing in the lib branches on the `is_custom_model` flag it
sets.

The weights come from scripts/convert_birdnet_2_4_onnx.py, which converts
upstream's SavedModel and writes into the same BIRDNET_APP_DATA layout the lib
uses for its own downloads. Labels are read from the onnx directory rather than
the tf one so that resolving a species name never triggers the 51 MB TFLite
download this engine no longer uses.

Unlike birdnet_lib, this module imports birdnet at module load. It can afford
to: nothing imports it until a v2.4 model is actually wanted, and by then the
lib is being loaded regardless. The backend classes have to be defined at module
level rather than built on demand, because the inference pipeline spawns worker
processes and pickles the backend type by reference. A class defined inside a
function cannot be pickled.

This module depends on birdnet internals that are not public API. The
equivalence test in tests/infrastructure/test_birdnet_2_4_onnx.py is what
catches a lib upgrade moving them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from birdnet.core.backends import (
    OnnxBackend,
    VersionedAcousticBackendProtocol,
    VersionedGeoBackendProtocol,
)
from birdnet.globals import MODEL_PRECISION_FP32
from birdnet.utils.helper import get_species_from_file
from birdnet.utils.local_data import get_lang_dir, get_model_path

from .birdnet_lib import MODEL_PRECISION

# v2.4 analyses 3 s at 48 kHz. Only consulted when a model's declared output
# shape is not static, which it is here, so this is a fallback.
SEGMENT_SIZE_SAMPLES = 144_000

# The geo model takes one (latitude, longitude, week) triplet per row.
GEO_INPUT_FEATURES = 3

# The geo model has no week axis of its own: the lib's v2.4 TF backend reports
# a single year-round input of -1.0, and the ONNX export has to agree or the
# lib would ask the model for weeks it cannot answer.
GEO_YEAR_ROUND_WEEK_INPUTS = (-1.0,)

_MISSING_MODEL_HINT = (
    "BirdNET v2.4 ONNX weights are missing at {path}.\n"
    "They are produced from upstream's SavedModel by:\n"
    "    uv run --script scripts/convert_birdnet_2_4_onnx.py\n"
    "Frozen builds convert during packaging, so this only affects running from source."
)


class AcousticOnnxBackendFP32V2_4(OnnxBackend, VersionedAcousticBackendProtocol):
    """v2.4 acoustic prediction head on onnxruntime.

    The export carries one output, the logits. Encoding is declared
    unsupported: the app only ever opens a predict_session, and the embeddings
    the TFLite backend exposed were an internal tensor read by index rather
    than a declared graph output, so there was nothing to carry over.
    """

    @classmethod
    def prediction_out_idx(cls) -> int:
        return 0

    @classmethod
    def supports_encoding(cls) -> bool:
        return False

    @classmethod
    def encoding_out_idx(cls) -> int | None:
        return None

    @classmethod
    def probe_input_size_samples(cls) -> int:
        return SEGMENT_SIZE_SAMPLES

    @classmethod
    def precision(cls) -> str:
        return MODEL_PRECISION_FP32


class GeoOnnxBackendFP32V2_4(OnnxBackend, VersionedGeoBackendProtocol):
    """v2.4 geo model on onnxruntime, supplying the per-site species list."""

    @classmethod
    def prediction_out_idx(cls) -> int:
        return 0

    @classmethod
    def supports_encoding(cls) -> bool:
        return False

    @classmethod
    def encoding_out_idx(cls) -> int | None:
        return None

    @classmethod
    def probe_input_size_samples(cls) -> int:
        return GEO_INPUT_FEATURES

    @classmethod
    def precision(cls) -> str:
        return MODEL_PRECISION_FP32

    @classmethod
    def year_round_week_inputs(cls) -> tuple[float, ...]:
        return GEO_YEAR_ROUND_WEEK_INPUTS


def model_path(kind: str) -> Path:
    """Where the converted weights live, for 'acoustic' or 'geo'.

    Resolved through the lib's own path helper so the converted files sit in
    the same BIRDNET_APP_DATA tree as everything else, and so a frozen build
    pointing that variable at its bundle finds them without special cases.
    """
    return get_model_path(kind, "2.4", "onnx", MODEL_PRECISION)


def labels(kind: str, lang: str):  # noqa: ANN201
    """The species list for one model and locale, as the lib's OrderedSet."""
    lang_file = get_lang_dir(kind, "2.4", "onnx") / f"{lang}.txt"
    if not lang_file.is_file():
        raise ValueError(f"Language does not exist for BirdNET v2.4: {lang}")
    return get_species_from_file(lang_file, encoding="utf8")


def _require(path: Path) -> Path:
    if not path.is_file():
        raise FileNotFoundError(_MISSING_MODEL_HINT.format(path=path))
    return path


def load_acoustic(lang: str = "en_us") -> Any:
    """Load the v2.4 acoustic model on onnxruntime."""
    from birdnet.acoustic.models.v2_4.model import AcousticModelV2_4

    return AcousticModelV2_4.load(
        _require(model_path("acoustic")),
        labels("acoustic", lang),
        backend_type=AcousticOnnxBackendFP32V2_4,
        backend_kwargs={},
    )


def load_geo(lang: str = "en_us") -> Any:
    """Load the v2.4 geo model on onnxruntime."""
    from birdnet.geo.models.v2_4.model import GeoModelV2_4

    return GeoModelV2_4.load(
        _require(model_path("geo")),
        labels("geo", lang),
        backend_type=GeoOnnxBackendFP32V2_4,
        backend_kwargs={},
    )
