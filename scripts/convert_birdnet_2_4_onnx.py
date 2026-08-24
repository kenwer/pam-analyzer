#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["birdnet>=1.1", "tensorflow", "tf2onnx", "onnx", "numpy"]
# ///
"""Convert BirdNET v2.4 from its TensorFlow SavedModel to ONNX.

Upstream ships v2.4 as TFLite and SavedModel only, so birdnet.load() rejects
the onnx backend for this version. The app needs it anyway: ai-edge-litert has
no Intel macOS wheel, which leaves v2.4 unable to load there, and it publishes
cp313 wheels only, which pins the project's Python ceiling. Converting here
lets both the acoustic and the geo model run on the same onnxruntime the v3.0
engine already uses.

This script runs on its own interpreter, declared above, because it is the only
place TensorFlow is needed. Keeping it out of pyproject.toml means the app
itself never installs TensorFlow. Python 3.12 rather than the app's 3.13
because that is the interpreter this toolchain is verified against.

Both models come from one download, BirdNET_v2.4_protobuf.zip, which carries
audio-model/, meta-model/ and a labels/ directory shared by both.

Two tf2onnx limitations have to be worked around, and only for the acoustic
model. Its mel frontend is frame -> Hann window -> rfft -> Cast(complex64 to
float32) -> mel filterbank. Casting complex to real in TensorFlow keeps the
real part and discards the imaginary one, so BirdNET uses a real-part
spectrogram rather than the usual magnitude one.

1. tf2onnx's FFT handler asserts its only consumer is ComplexAbs and refuses a
   Cast. The assertion is relaxed below.
2. tf2onnx's Cast handler has no complex-source case, and by the time handlers
   run, the SrcT and DstT attributes have been normalised away to a plain 'to'.
   The complex Casts are therefore identified on the TensorFlow graph, where
   the dtypes are still intact, and rewritten during conversion by name.

tf2onnx represents an FFT result as a float tensor with real and imaginary
parts stacked on a new leading axis of size 2, so discarding the imaginary part
is Gather(axis=0, index=0) followed by Squeeze.

Usage:
    uv run --script scripts/convert_birdnet_2_4_onnx.py
    uv run --script scripts/convert_birdnet_2_4_onnx.py --force
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import tf2onnx
from tf2onnx import tf_loader, utils
from tf2onnx.graph_builder import GraphBuilder

# tf2onnx's RFFT handler tops out at version_13 and builds the transform from
# constant cos/sin matrices. ONNX's native DFT op, added in opset 17, is never
# reached, so raising the opset past 13 buys nothing here.
OPSET = 13

# TensorFlow's DataType enum. Only the complex entries matter below.
DT_COMPLEX64 = 8
DT_COMPLEX128 = 18

FFT_CONSUMER_ASSERT = "Current implementation of RFFT or FFT only allows ComplexAbs as consumer"

# Written beside each converted model so a rerun can tell an up-to-date file
# from one produced by an older toolchain. Sizes cannot serve here the way they
# do for birdnet's own downloads, because tf2onnx output is not byte-reproducible
# across platforms.
MARKER_NAME = ".conversion.json"

_real_make_sure = utils.make_sure


def _relaxed_make_sure(bool_val, error_msg, *args):
    """utils.make_sure with the FFT consumer assertion suppressed.

    Every other assertion still fires. Patching the function rather than the
    handler keeps this to one narrowly-matched message.
    """
    if not bool_val and FFT_CONSUMER_ASSERT in error_msg:
        return None
    return _real_make_sure(bool_val, error_msg, *args)


def _find_complex_casts(graph_def) -> set[str]:
    """Names of Cast nodes reading a complex tensor.

    Read off the TensorFlow graph because SrcT survives only there.
    """
    return {
        node.name
        for node in graph_def.node
        if node.op == "Cast" and node.attr["SrcT"].type in (DT_COMPLEX64, DT_COMPLEX128)
    }


def _make_cast_handler(complex_casts: set[str], rewritten: list[str]):
    """Build a tf2onnx Cast handler that emits the real part for complex input."""

    def cast_handler(ctx, node, name, args):
        if node.name not in complex_casts:
            # The builtin handler is a no-op at opset 6 and above: 'to' is
            # already set when the graph is built.
            return
        index_zero = ctx.make_const(utils.make_name("cst0"), np.array([0], dtype=np.int64))
        real = ctx.make_node(
            "Gather",
            inputs=[node.input[0], index_zero.name],
            attr={"axis": 0},
            name=utils.make_name("RealPart_" + node.name),
        )
        ctx.remove_node(node.name)
        squeezed = GraphBuilder(ctx).make_squeeze(
            {"data": real.output[0], "axes": [0]},
            name=utils.make_name("RealPartSq_" + node.name),
            return_node=True,
        )
        ctx.replace_all_inputs(node.output[0], squeezed.output[0])
        rewritten.append(node.name)

    return cast_handler


def _convert_one(saved_model_dir: Path, signature: str, out_path: Path) -> int:
    """Convert one SavedModel signature to ONNX. Returns the rewrite count."""
    graph_def, inputs, outputs = tf_loader.from_saved_model(
        str(saved_model_dir), None, None, tag="serve", signatures=[signature]
    )
    complex_casts = _find_complex_casts(graph_def)
    rewritten: list[str] = []

    out_path.parent.mkdir(parents=True, exist_ok=True)
    utils.make_sure = _relaxed_make_sure
    try:
        tf2onnx.convert.from_graph_def(
            graph_def,
            input_names=inputs,
            output_names=outputs,
            opset=OPSET,
            custom_op_handlers={"Cast": (_make_cast_handler(complex_casts, rewritten), [])},
            output_path=str(out_path),
        )
    finally:
        utils.make_sure = _real_make_sure

    missed = complex_casts - set(rewritten)
    if missed:
        # Silently leaving one in place produces a model that converts cleanly
        # and then fails at the mel matmul with a shape mismatch, so this is
        # worth failing loudly on.
        raise SystemExit(f"complex Cast nodes not rewritten: {sorted(missed)}")
    return len(rewritten)


def _marker(out_path: Path) -> Path:
    return out_path.parent / MARKER_NAME


def _marker_payload(source_url: str) -> dict:
    return {
        "source": source_url,
        "opset": OPSET,
        "tf2onnx": tf2onnx.__version__,
    }


def _is_current(out_path: Path, source_url: str) -> bool:
    """Whether out_path was produced from this source by this toolchain."""
    marker = _marker(out_path)
    if not out_path.is_file() or not marker.is_file():
        return False
    try:
        return json.loads(marker.read_text()) == _marker_payload(source_url)
    except (OSError, ValueError):
        return False


def _discard_source(saved_model_dir: Path) -> None:
    """Delete the SavedModel the conversion read from.

    It is an intermediate, and the whole app-data tree is bundled into the
    frozen app by a single --add-data entry, so anything left here ships to
    users. The two v2.4 SavedModels come to about 155 MB, none of which the app
    can load: it has no TensorFlow. Reconverting re-downloads them, which the
    marker written beside each output makes rare.
    """
    shutil.rmtree(saved_model_dir.parent, ignore_errors=True)


def _install_labels(src_lang_dir: Path, dest_lang_dir: Path) -> int:
    """Copy the per-locale label files next to the converted model.

    Giving the onnx backend its own label directory is what lets the app read
    v2.4 labels without touching the TFLite download they otherwise live beside.
    """
    dest_lang_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(dest_lang_dir, ignore_errors=True)
    shutil.copytree(src_lang_dir, dest_lang_dir)
    return len(list(dest_lang_dir.glob("*.txt")))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="reconvert even when the output is current")
    parser.add_argument(
        "--keep-sources",
        action="store_true",
        help="keep the downloaded SavedModels instead of deleting them after conversion",
    )
    args = parser.parse_args()

    # The download URL is a module-level constant upstream, one per model, and
    # both currently name the same zip. They are read separately rather than
    # shared so that a future upstream split cannot go unnoticed.
    from birdnet.acoustic.models.v2_4.pb import _PB_DL_URL as ACOUSTIC_PB_URL
    from birdnet.acoustic.models.v2_4.pb import AcousticPBDownloaderV2_4
    from birdnet.geo.models.v2_4.pb import _PB_DL_URL as GEO_PB_URL
    from birdnet.geo.models.v2_4.pb import GeoPBDownloaderV2_4
    from birdnet.utils.local_data import get_lang_dir, get_model_path

    jobs = (
        ("acoustic", AcousticPBDownloaderV2_4, "basic", ACOUSTIC_PB_URL),
        ("geo", GeoPBDownloaderV2_4, "serving_default", GEO_PB_URL),
    )

    for kind, downloader, signature, source_url in jobs:
        out_path = get_model_path(kind, "2.4", "onnx", "fp32")

        if not args.force and _is_current(out_path, source_url):
            print(f"  {kind} v2.4 onnx is current, skipping")
            continue

        # Downloads and unzips on a cold cache. It reads label files and moves
        # directories, and pulls in no TensorFlow: only PBBackend.load() does
        # that, and this never loads a backend.
        print(f"  Fetching {kind} v2.4 SavedModel")
        saved_model_dir, _labels = downloader.get_model_path_and_labels("en_us")

        print(f"  Converting {kind} v2.4 ({signature}) to onnx opset {OPSET}")
        rewrites = _convert_one(Path(saved_model_dir), signature, out_path)

        n_labels = _install_labels(get_lang_dir(kind, "2.4", "pb"), get_lang_dir(kind, "2.4", "onnx"))
        _marker(out_path).write_text(json.dumps(_marker_payload(source_url), indent=2))

        size_mb = out_path.stat().st_size / 1e6
        detail = f", {rewrites} real-part rewrites" if rewrites else ""
        print(f"  Wrote {out_path.name} ({size_mb:.1f} MB, {n_labels} locales{detail})")

        if not args.keep_sources:
            _discard_source(Path(saved_model_dir))

    print("v2.4 onnx models ready.")


if __name__ == "__main__":
    sys.exit(main())
