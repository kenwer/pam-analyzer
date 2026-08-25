"""Contract tests for the locally-defined Perch v2 ONNX backend.

The birdnet lib knows Perch only as a TensorFlow SavedModel: its loader rejects
every backend but 'pb' for this model, and the SavedModel is a jax2tf export
whose whole graph sits inside XlaCallModule ops. This app runs a third-party
ONNX conversion of it instead, so the backend pairing it with the lib's own
AcousticModelPerchV2 is defined here rather than upstream.

What these tests pin is the pairing, not the arithmetic: which graph output is
the class head, how wide a window the model expects, and that the class can
survive the pickling the inference pipeline puts it through.
"""

from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
import pytest

from pam_analyzer.infrastructure import perch_onnx

# Output order of the converted graph, from its own metadata:
#   0 embedding [batch, 1536]
#   1 spatial_embedding [batch, 16, 4, 1536]
#   2 spectrogram [batch, 500, 128]
#   3 label [batch, 14795]
LABEL_OUT_IDX = 3
EMBEDDING_OUT_IDX = 0

# 5.0 s at 32 kHz, matching AcousticModelPerchV2.get_segment_size_samples().
SEGMENT_SAMPLES = 160_000

PERCH_CLASSES = 14795

# Agreement required between a windowed and a batched run of the same audio.
# Perch emits raw logits, and the runner thresholds them in logit space, so the
# comparison stays in the units the model produces.
LOGIT_TOLERANCE = 1e-4


def _load_backend():  # noqa: ANN202
    """Instantiate the backend the way birdnet's BackendLoader does."""
    backend = perch_onnx.AcousticOnnxBackendFP32PerchV2(
        model_path=perch_onnx.require_weights(perch_onnx.model_path()),
        device_name="CPU",
        half_precision=False,
    )
    backend.load()
    return backend


def _predict(backend, x):  # noqa: ANN001, ANN202
    return backend.copy_from_device(backend.predict(backend.copy_to_device(x)))


def test_backend_reads_the_label_output_for_predictions() -> None:
    """The class head is output 3, not output 0 as in the BirdNET exports.

    Perch's conversion exports four tensors and puts the embedding first, so a
    backend that defaulted to index 0 would score every window against a 1536
    dimensional embedding and never raise, just produce nonsense.
    """
    assert perch_onnx.AcousticOnnxBackendFP32PerchV2.prediction_out_idx() == LABEL_OUT_IDX


def test_backend_exposes_the_embedding_output_for_encoding() -> None:
    """Perch is also an embedding model, and the export keeps that tensor."""
    backend = perch_onnx.AcousticOnnxBackendFP32PerchV2
    assert backend.supports_encoding() is True
    assert backend.encoding_out_idx() == EMBEDDING_OUT_IDX


def test_backend_probes_with_a_five_second_window() -> None:
    """The probe input has to match the window the lib frames audio into.

    OnnxBackend.n_species falls back to a probe inference when the declared
    output shape is not static. A 3 s BirdNET window would be rejected by the
    graph, so the wrong constant here surfaces only on that fallback path.
    """
    assert perch_onnx.AcousticOnnxBackendFP32PerchV2.probe_input_size_samples() == SEGMENT_SAMPLES


def test_backend_type_is_picklable() -> None:
    """predict_session pickles the backend type to reach its worker processes.

    A class defined inside a function cannot be addressed by pickle, which
    fails at worker startup rather than at import, so it is worth pinning
    without paying for a full run.
    """
    backend = perch_onnx.AcousticOnnxBackendFP32PerchV2
    assert pickle.loads(pickle.dumps(backend)) is backend


def test_labels_cover_every_class_the_model_emits() -> None:
    """The species list and the class head have to be the same width.

    AcousticModelPerchV2 indexes the species list by the model's output
    position, so a list that drifted from the graph would mislabel every
    detection rather than fail.
    """
    labels = perch_onnx.labels()
    assert len(labels) == PERCH_CLASSES


def test_labels_exclude_the_header_row() -> None:
    """Perch ships its labels as a CSV whose first line names the label set.

    That line ('inat2024_fsd50k') is not a species, and leaving it in would
    shift every class index by one.
    """
    assert "inat2024_fsd50k" not in perch_onnx.labels()


def test_model_path_sits_beside_the_libs_own_perch_download() -> None:
    """Weights land in the layout birdnet uses for Perch, under an onnx subdir.

    A frozen build points BIRDNET_APP_DATA at its bundle, so resolving through
    the same tree is what lets the packaged app find the file with no special
    case.
    """
    path = perch_onnx.model_path()
    assert path.parent.parent.name == "perch-v2"
    assert path.parent.name == "onnx"


def test_missing_weights_report_how_to_fetch_them() -> None:
    """Running from source without the download should say what to run.

    The weights are a 413 MB third-party artifact rather than something the
    build converts, so the failure has to name the fetch step instead of
    surfacing as a bare onnxruntime load error.
    """
    missing = perch_onnx.model_path().with_name("definitely-absent.onnx")
    with pytest.raises(FileNotFoundError, match="fetch_perch_onnx"):
        perch_onnx.require_weights(missing)


@pytest.mark.slow
def test_model_emits_one_score_per_label() -> None:
    """The class head and the vendored label list have to agree in width.

    AcousticModelPerchV2 indexes the species list by output position, so a
    mismatch between the shipped labels and the shipped weights would mislabel
    every detection instead of raising.
    """
    backend = _load_backend()
    scores = _predict(backend, np.zeros((1, SEGMENT_SAMPLES), dtype=np.float32))
    assert scores.shape == (1, PERCH_CLASSES)
    assert len(perch_onnx.labels()) == scores.shape[1]


@pytest.mark.slow
def test_batching_does_not_change_scores() -> None:
    """A window scores the same alone as inside a batch.

    The runner opens its session with batch_size=8, so the batch axis has to be
    genuinely dynamic rather than a fixed dimension the export baked in.
    """
    backend = _load_backend()
    rng = np.random.default_rng(0)
    window = rng.standard_normal((1, SEGMENT_SAMPLES), dtype=np.float32) * 0.1

    single = _predict(backend, window)
    batched = _predict(backend, np.repeat(window, 4, axis=0))

    assert batched.shape[0] == 4
    assert np.abs(batched - single).max() < LOGIT_TOLERANCE


@pytest.mark.slow
def test_inference_is_deterministic() -> None:
    """Two runs of one input agree bit for bit, so an analysis is reproducible."""
    backend = _load_backend()
    rng = np.random.default_rng(1)
    window = rng.standard_normal((1, SEGMENT_SAMPLES), dtype=np.float32) * 0.1
    assert np.array_equal(_predict(backend, window), _predict(backend, window))


def test_fetch_script_and_app_agree_on_the_model_path() -> None:
    """The fetch script writes exactly where the app looks.

    scripts/fetch_perch_onnx.py runs on its own interpreter and cannot import
    this package, so it rebuilds the path from birdnet's APP_DIR. That
    duplication is only safe while the two stay identical, and a mismatch would
    surface as a missing-weights error after an apparently successful fetch.
    """
    source = (
        Path(__file__).parent.parent.parent / "scripts" / "fetch_perch_onnx.py"
    ).read_text(encoding="utf-8")

    app_path = perch_onnx.model_path()
    tail = app_path.relative_to(app_path.parents[3])
    quoted = " / ".join(f'"{part}"' for part in tail.parts)
    assert f"APP_DIR / {quoted}" in source
