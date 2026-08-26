"""ONNX backend for Perch v2.

`birdnet.load_perch_v2()` can only return the TensorFlow SavedModel: the lib
declares Perch pb-only and refuses the onnx backend outright. That SavedModel is
a jax2tf export with native serialization, so its entire compute graph sits
inside nine XlaCallModule ops carrying serialized StableHLO. tf2onnx walks
TensorFlow ops one at a time and there are none to walk, which is why the
conversion that works for BirdNET v2.4 cannot work here, and why the app depends
on a third-party export instead of converting during the build.

The weights come from justinchuby/Perch-onnx on Hugging Face, pinned by commit
and checksum in scripts/build.py. That export reached ONNX the long way around,
via TFLite, whose MLIR converter can see inside XlaCallModule.

What makes the pairing possible is the same property birdnet_2_4_onnx relies on:
a birdnet "backend" is the only version-specific part. `OnnxBackend` is generic
over a session and output indices, while the model class holds the preprocessing
contract, which for Perch is 32 kHz and 160000 samples per 5 s window. Pairing a
locally-defined backend with the lib's own `AcousticModelPerchV2` therefore
changes the inference runtime and nothing else, and the lib's predict_session
pipeline keeps doing the audio I/O, framing and batching.

Two things differ from the BirdNET backends. The class head is output 3 rather
than output 0, because the export declares the embedding first. And encoding is
genuinely supported, because Perch is an embedding model whose 1536-dimensional
output the conversion kept as a declared graph output.

Labels ship with this package rather than with the weights. The Hugging Face
export carries none, and the only upstream source is a 379 MB download the app
would otherwise never need. data/perch_v2_labels.csv is a verbatim copy of the
assets/labels.csv in Google's Kaggle release, header row included, which is
stripped below.

This module depends on birdnet internals that are not public API. The tests in
tests/infrastructure/test_perch_onnx.py are what catch a lib upgrade moving them.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files
from pathlib import Path

from birdnet.core.backends import OnnxBackend, VersionedAcousticBackendProtocol
from birdnet.globals import MODEL_PRECISION_FP32
from birdnet.utils.local_data import APP_DIR

from .birdnet_onnx_threads import DEFAULT_SESSION_THREADS, pin_session_threads

# Output positions in the converted graph. The export declares, in order:
# embedding [batch, 1536], spatial_embedding [batch, 16, 4, 1536],
# spectrogram [batch, 500, 128], label [batch, 14795].
LABEL_OUT_IDX = 3
EMBEDDING_OUT_IDX = 0

# 5.0 s at 32 kHz. Only consulted when the declared output shape is not static,
# which it is here, so this is a fallback.
SEGMENT_SIZE_SAMPLES = 160_000

# Names the label set rather than a species, and has to come off the front so
# class indices line up with the model's output positions.
_LABELS_HEADER = "inat2024_fsd50k"

_LABELS_FILE = "perch_v2_labels.csv"

_MISSING_MODEL_HINT = (
    "Perch v2 ONNX weights are missing at {path}.\n"
    "They are a third-party export rather than something the build converts:\n"
    "    uv run --script scripts/fetch_perch_onnx.py\n"
    "Frozen builds fetch during packaging, so this only affects running from source."
)


class AcousticOnnxBackendFP32PerchV2(OnnxBackend, VersionedAcousticBackendProtocol):
    """Perch v2's prediction and embedding heads on onnxruntime.

    Defined at module level rather than built on demand because the inference
    pipeline spawns worker processes and pickles the backend type by reference.
    A class defined inside a function cannot be pickled.
    """

    @classmethod
    def prediction_out_idx(cls) -> int:
        return LABEL_OUT_IDX

    @classmethod
    def supports_encoding(cls) -> bool:
        return True

    @classmethod
    def encoding_out_idx(cls) -> int | None:
        return EMBEDDING_OUT_IDX

    @classmethod
    def probe_input_size_samples(cls) -> int:
        return SEGMENT_SIZE_SAMPLES

    @classmethod
    def precision(cls) -> str:
        return MODEL_PRECISION_FP32


@cache
def labels():  # noqa: ANN201
    """Perch's 14795 class names, in model output order, as the lib's OrderedSet.

    Plain scientific names, unlike BirdNET's 'Scientific_Common' entries: Perch
    ships no common names and no locales of its own.
    """
    from ordered_set import OrderedSet

    text = files(__package__).joinpath("data", _LABELS_FILE).read_text(encoding="utf-8")
    names = OrderedSet(line.strip() for line in text.splitlines() if line.strip())
    names.remove(_LABELS_HEADER)
    return names


@cache
def label_set() -> frozenset[str]:
    """Immutable set of species label names for membership tests.

    A Perch run asks this both to classify a filter drop and to tell one of
    the underscored sound-event classes apart from a BirdNET list entry.
    """
    return frozenset(labels())


def model_path() -> Path:
    """Where the fetched weights live.

    Under the lib's own Perch folder rather than its versioned acoustic-models
    layout, because `get_model_path` takes a declared version literal and Perch
    is not one of them. The onnx subdirectory keeps this apart from the
    SavedModel the lib would download for its pb backend.
    """
    return APP_DIR / "acoustic-models" / "perch-v2" / "onnx" / "model-fp32.onnx"


def require_weights(path: Path) -> Path:
    """Return `path`, or explain how to fetch it.

    Separate from load_acoustic so the message is reachable without building a
    session, and so the check reads the same from a test as from the app.
    """
    if not path.is_file():
        raise FileNotFoundError(_MISSING_MODEL_HINT.format(path=path))
    return path


def load_acoustic(threads: int = DEFAULT_SESSION_THREADS):  # noqa: ANN201
    """Load Perch v2 on onnxruntime, paired with the lib's own model class.

    `AcousticModelPerchV2` supplies the preprocessing contract (32 kHz, 5 s,
    160000 samples) and the whole predict_session pipeline. Only the backend
    changes, so the lib never learns that its SavedModel is not what is running.

    `load()` rather than `load_custom()` because it takes `backend_type`
    directly and skips the '.onnx' suffix check and the `check_validity`
    subprocess spawn that would load the 413 MB model a second time.

    `threads` sizes each inference worker's session and belongs with the
    n_workers the caller opens its session with, so the runner passes it.
    """
    from birdnet.acoustic.models.perch_v2.model import AcousticModelPerchV2

    return pin_session_threads(
        AcousticModelPerchV2.load(
            require_weights(model_path()),
            labels(),
            backend_type=AcousticOnnxBackendFP32PerchV2,
            backend_kwargs={},
        ),
        threads=threads,
    )
