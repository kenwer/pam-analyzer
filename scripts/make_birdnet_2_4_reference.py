#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = ["birdnet>=1.1,<1.2", "ai-edge-litert", "numpy"]
# ///
"""Capture BirdNET v2.4 reference output from the TFLite model on ai-edge-litert.

The app runs v2.4 on ONNX, converted by convert_birdnet_2_4_onnx.py. Nothing in
the app loads the TFLite weights any more, so this file is what pins the
converted model to the numbers the litert build produced. The equivalence test
in tests/infrastructure/test_birdnet_2_4_onnx.py reads the fixture this writes
and fails if a converted model drifts from it, on any platform.

ai-edge-litert is declared here rather than in pyproject.toml because this is
the only code that still needs it. The project actively overrides it away, so
this PEP 723 header, which resolves on its own, is the only place it is
installed. It has no Intel macOS wheel, which is part of why the app moved off
it, so this script cannot be run there.

The fixture stores its inputs rather than a seed, so the test never has to
reproduce a generator and cannot silently drift from one.

Usage:
    uv run --script scripts/make_birdnet_2_4_reference.py
"""

from pathlib import Path

import numpy as np
from ai_edge_litert import interpreter as litert
from birdnet.acoustic.models.v2_4.tf import AcousticTFBackendFP32V2_4
from birdnet.geo.models.v2_4.tf import GeoTFBackendFP32V2_4
from birdnet.utils.local_data import get_model_path

ROOT = Path(__file__).parent.parent
OUT = ROOT / "tests" / "data" / "birdnet_2_4_reference.npz"

# Resolved through birdnet's own helper so this honours BIRDNET_APP_DATA and
# finds the weights on Windows and Linux as well.
ACOUSTIC_TFLITE = get_model_path("acoustic", "2.4", "tf", "fp32")
GEO_TFLITE = get_model_path("geo", "2.4", "tf", "fp32")

# Read off the lib's own TFLite backends rather than hardcoded, so the
# reference is captured from the exact tensors the app used to read.
ACOUSTIC_OUT_IDX = AcousticTFBackendFP32V2_4.prediction_out_idx()
GEO_OUT_IDX = GeoTFBackendFP32V2_4.prediction_out_idx()

SEGMENT_SAMPLES = 144_000  # 3.0 s at 48 kHz

# Seed 0 at amplitude 0.05 matches _write_noise_wav in the existing suite, so
# the reference window is the same kind of signal those tests already feed.
NOISE_SEED = 0
NOISE_AMPLITUDE = 0.05

# Real coordinates rather than round numbers, so the geo model returns a
# plausible species set instead of an ocean-tile degenerate one.
GEO_INPUTS = np.array(
    [
        [48.52, 9.06, 20.0],  # Tuebingen, week 20
        [48.52, 9.06, -1.0],  # same place, no week filter
        [-3.47, -62.37, 35.0],  # central Amazon, week 35
    ],
    dtype=np.float32,
)


def _load(path: Path) -> litert.Interpreter:
    if not path.is_file():
        raise SystemExit(f"missing {path}. Run the app once on v2.4 before this build, or restore the TFLite cache.")
    interp = litert.Interpreter(
        str(path),
        num_threads=1,
        experimental_op_resolver_type=litert.OpResolverType.BUILTIN_WITHOUT_DEFAULT_DELEGATES,
    )
    interp.allocate_tensors()
    return interp


def _infer(interp: litert.Interpreter, x: np.ndarray, out_idx: int) -> np.ndarray:
    interp.resize_tensor_input(0, x.shape, strict=True)
    interp.allocate_tensors()
    interp.set_tensor(0, x)
    interp.invoke()
    return interp.get_tensor(out_idx).copy()


def main() -> None:
    rng = np.random.default_rng(NOISE_SEED)
    audio = np.ascontiguousarray(
        rng.standard_normal((1, SEGMENT_SAMPLES)) * NOISE_AMPLITUDE,
        dtype=np.float32,
    )

    acoustic_logits = _infer(_load(ACOUSTIC_TFLITE), audio, ACOUSTIC_OUT_IDX)
    geo_scores = _infer(_load(GEO_TFLITE), GEO_INPUTS, GEO_OUT_IDX)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        OUT,
        audio=audio,
        acoustic_logits=acoustic_logits,
        geo_inputs=GEO_INPUTS,
        geo_scores=geo_scores,
    )

    size_kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT.relative_to(ROOT)} ({size_kb:.0f} KB)")
    print(f"  acoustic {audio.shape} -> {acoustic_logits.shape}, logits {acoustic_logits.min():.3f}..{acoustic_logits.max():.3f}")
    print(f"  geo      {GEO_INPUTS.shape} -> {geo_scores.shape}, scores {geo_scores.min():.3f}..{geo_scores.max():.3f}")


if __name__ == "__main__":
    main()
