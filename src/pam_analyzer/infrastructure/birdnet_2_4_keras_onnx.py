"""Build BirdNET v2.4 ONNX models from upstream's Keras weights, without TensorFlow.

birdnet_2_4_convert downloads the Keras zip and calls convert() on it. This
module imports onnx and h5py, so nothing imports it until a conversion runs.

Upstream ships v2.4 as TFLite, SavedModel, Keras and TF.js, but not ONNX. The
Keras release, BirdNET_v2.4_keras.zip in Zenodo record 15050749, has both models
as HDF5 files. Each holds its architecture as JSON and its weights as plain
arrays, so the graph can be rebuilt in ONNX layer by layer with numpy and
onnx.helper. On one machine the same zip gives byte-identical models. Across
CPUs the bytes may differ in the last bit of a few constants, because numpy's
float32 SIMD paths differ, so the app does not pin the output hashes.

Each model has one custom layer, and the builder hand-writes both.

MelSpecLayerSimple (acoustic, twice). The zip ships its source. It min-max
normalises each window to [-1, 1], takes an STFT with a periodic Hann window,
casts the complex result to float32, which keeps the real part rather than the
magnitude, applies a mel filterbank from tf.signal.linear_to_mel_weight_matrix,
squares, raises to 1 / (1 + exp(magnitude_scaling)), flips the mel axis, and
transposes to (mels, frames). The filterbank is ported to numpy and matches
TensorFlow's to a few ulps, as closely as upstream's own exports match each
other. The windowed real DFT is a strided Conv1D whose kernels are
window * cos(2 pi k n / N), so framing and transform are one op. The flip is
folded into the filterbank.

MDataLayer (geo). The zip has no source. It was recovered from the graph the
SavedModel holds. Latitude, longitude and week are scaled to [0, 1] as
(lat + 90) / 180, (lon + 180) / 360 and (week - 1) / 48, and each becomes
sqrt(2) * sin(2 pi x + phase) over 'embeddings' phases spaced pi / embeddings
apart. The week embedding is zeroed unless 0 < week < 49, which is how week -1
asks for a year-round list.

The rest of both models is standard Keras layers. Keras is channels-last and
ONNX convolutions are channels-first, so the acoustic graph is built in NCHW
from the mel output on, and the kernels are transposed to match.

tests/infrastructure/test_birdnet_2_4_onnx.py checks the output against the
reference captured from the TFLite model.
"""

import json
import logging
import math
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

import h5py
import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

# (file in the zip, layer whose output the ONNX model returns). The acoustic
# graph is cut before its final sigmoid because birdnet's OnnxBackend expects
# logits, as the SavedModel's 'basic' signature and the TFLite model return.
# The geo model returns probabilities.
MODELS = {
    "acoustic": ("audio-model.h5", "CLASS_DENSE_LAYER"),
    "geo": ("meta-model.h5", "MNET_CLASS_ACTIVATION"),
}

# The opset tf2onnx emitted for the previous conversion, and an IR version
# every supported onnxruntime reads
OPSET = 13
IR_VERSION = 8

# MelSpecLayerSimple adds this to the window maximum before dividing
NORM_EPSILON = 1e-6


class GraphBuilder:
    """Collects ONNX nodes and initializers, and tracks static per-sample shapes.

    Shapes exclude the batch axis. Rank-3 shapes are (C, H, W) in NCHW.
    """

    def __init__(self) -> None:
        self.nodes: list[onnx.NodeProto] = []
        self.initializers: list[onnx.TensorProto] = []
        self.shapes: dict[str, tuple[int, ...]] = {}
        self._names: set[str] = set()

    def _unique(self, name: str) -> str:
        candidate, i = name, 1
        while candidate in self._names:
            candidate, i = f"{name}_{i}", i + 1
        self._names.add(candidate)
        return candidate

    def const(self, name: str, value: np.ndarray) -> str:
        name = self._unique(name)
        self.initializers.append(numpy_helper.from_array(np.ascontiguousarray(value), name))
        return name

    def op(self, op_type: str, inputs: list[str], name: str, shape: tuple[int, ...] | None = None, **attrs) -> str:
        out = self._unique(name)
        self.nodes.append(helper.make_node(op_type, inputs, [out], name=out, **attrs))
        if shape is not None:
            self.shapes[out] = shape
        return out

    def activation(self, x: str, kind: str, name: str) -> str:
        shape = self.shapes[x]
        if kind == "linear":
            return x
        if kind == "relu":
            return self.op("Relu", [x], name, shape)
        if kind == "sigmoid":
            return self.op("Sigmoid", [x], name, shape)
        if kind == "swish":
            gate = self.op("Sigmoid", [x], f"{name}_sigmoid", shape)
            return self.op("Mul", [x, gate], name, shape)
        raise ValueError(f"unsupported activation {kind!r} in {name}")

    def model(self, input_name: str, input_features: int, output: str, graph_name: str) -> onnx.ModelProto:
        graph = helper.make_graph(
            self.nodes,
            graph_name,
            [helper.make_tensor_value_info(input_name, TensorProto.FLOAT, ["batch", input_features])],
            [helper.make_tensor_value_info(output, TensorProto.FLOAT, ["batch", *self.shapes[output]])],
            self.initializers,
        )
        model = helper.make_model(
            graph,
            opset_imports=[helper.make_opsetid("", OPSET)],
            producer_name="pam-analyzer birdnet_2_4_keras_onnx",
        )
        model.ir_version = IR_VERSION
        return model


def _same_pads(size: int, kernel: int, stride: int) -> tuple[int, int, int]:
    """TensorFlow 'same' padding for one axis: (out, before, after). The odd pixel goes after."""
    out = -(-size // stride)
    total = max((out - 1) * stride + kernel - size, 0)
    return out, total // 2, total - total // 2


def _spatial(cfg: dict, h: int, w: int, kernel: list[int], strides: list[int]) -> tuple[int, int, list[int]]:
    """Output height and width plus ONNX pads for a conv or pool layer."""
    if cfg.get("dilation_rate", [1, 1]) != [1, 1]:
        raise ValueError(f"dilation is not supported: {cfg['name']}")
    if cfg["padding"] == "valid":
        return (h - kernel[0]) // strides[0] + 1, (w - kernel[1]) // strides[1] + 1, [0, 0, 0, 0]
    out_h, top, bottom = _same_pads(h, kernel[0], strides[0])
    out_w, left, right = _same_pads(w, kernel[1], strides[1])
    return out_h, out_w, [top, left, bottom, right]


def _hertz_to_mel(f: np.ndarray) -> np.ndarray:
    return np.float32(1127.0) * np.log(np.float32(1.0) + f / np.float32(700.0))


def _linspace(start: float, stop: float, num: int) -> np.ndarray:
    """tf.linspace's float32 arithmetic, which np.linspace rounds differently."""
    start, stop = np.float32(start), np.float32(stop)
    step = (stop - start) / np.float32(num - 1)
    out = start + np.arange(num, dtype=np.float32) * step
    out[-1] = stop
    return out


def mel_filterbank(mels: int, bins: int, sample_rate: int, fmin: float, fmax: float) -> np.ndarray:
    """tf.signal.linear_to_mel_weight_matrix in float32, shape (bins, mels).

    The layer builds its filterbank with that function at load time, and the
    Keras 2.5 file stores only its arguments. The DC bin gets no weight.
    """
    hz = _linspace(0.0, sample_rate / 2, bins)[1:]
    bin_mels = _hertz_to_mel(hz)[:, None]
    edges = _linspace(_hertz_to_mel(np.float32(fmin)), _hertz_to_mel(np.float32(fmax)), mels + 2)
    lower, center, upper = edges[:-2], edges[1:-1], edges[2:]
    rising = (bin_mels - lower) / (center - lower)
    falling = (upper - bin_mels) / (upper - center)
    weights = np.maximum(np.float32(0.0), np.minimum(rising, falling))
    return np.pad(weights, [[1, 0], [0, 0]]).astype(np.float32)


def _mel_spec(b: GraphBuilder, cfg: dict, weights: dict, audio: str, n_samples: int) -> str:
    """MelSpecLayerSimple, emitting (1, mels, frames) per sample."""
    name = cfg["name"]
    n, step = cfg["frame_length"], cfg["frame_step"]
    mels, bins = cfg["spec_shape"][0], n // 2 + 1
    filterbank = mel_filterbank(mels, bins, cfg["sample_rate"], cfg["fmin"], cfg["fmax"])
    frames = (n_samples - n) // step + 1
    if cfg["data_format"] != "channels_last":
        raise ValueError(f"unexpected mel layer config: {name}")

    lo = b.op("ReduceMin", [audio], f"{name}/min", (1,), axes=[1], keepdims=1)
    shifted = b.op("Sub", [audio, lo], f"{name}/shift", (n_samples,))
    hi = b.op("ReduceMax", [shifted], f"{name}/max", (1,), axes=[1], keepdims=1)
    hi = b.op("Add", [hi, b.const(f"{name}/eps", np.float32(NORM_EPSILON))], f"{name}/max_eps", (1,))
    x = b.op("Div", [shifted, hi], f"{name}/scale", (n_samples,))
    x = b.op("Sub", [x, b.const(f"{name}/half", np.float32(0.5))], f"{name}/center", (n_samples,))
    x = b.op("Mul", [x, b.const(f"{name}/two", np.float32(2.0))], f"{name}/normalized", (n_samples,))
    x = b.op("Unsqueeze", [x, b.const(f"{name}/axis1", np.array([1], np.int64))], f"{name}/channel", (1, n_samples))

    # Reducing k * n modulo N before the cosine keeps the angle exact for large k * n
    sample = np.arange(n, dtype=np.int64)
    angle = 2.0 * np.pi * ((np.arange(bins, dtype=np.int64)[:, None] * sample[None, :]) % n) / n
    hann = 0.5 - 0.5 * np.cos(2.0 * np.pi * sample / n)
    kernel = (np.cos(angle) * hann[None, :]).astype(np.float32)[:, None, :]
    spec = b.op("Conv", [x, b.const(f"{name}/dft_real", kernel)], f"{name}/stft_real", (bins, frames), strides=[step])

    flipped = np.ascontiguousarray(filterbank[:, ::-1].T)
    spec = b.op("MatMul", [b.const(f"{name}/filterbank", flipped), spec], f"{name}/mel", (mels, frames))
    spec = b.op("Mul", [spec, spec], f"{name}/power", (mels, frames))
    mag_scale = np.float32(weights["magnitude_scaling"])
    exponent = np.float32(1.0) / (np.float32(1.0) + np.exp(mag_scale))
    spec = b.op("Pow", [spec, b.const(f"{name}/exponent", exponent)], f"{name}/compress", (mels, frames))
    return b.op("Unsqueeze", [spec, b.const(f"{name}/axis1", np.array([1], np.int64))], name, (1, mels, frames))


def _mdata(b: GraphBuilder, cfg: dict, x: str) -> str:
    """MDataLayer, embedding (latitude, longitude, week) as 3 * embeddings sinusoids."""
    name = cfg["name"]
    k = cfg["embeddings"]
    phases = b.const(f"{name}/phases", (np.arange(k) * np.pi / k).astype(np.float32))
    two_pi = b.const(f"{name}/two_pi", np.float32(2.0 * np.pi))
    sqrt2 = b.const(f"{name}/sqrt2", np.float32(math.sqrt(2.0)))
    split = b.const(f"{name}/split", np.array([1, 1, 1], np.int64))
    columns = [f"{name}/lat", f"{name}/lon", f"{name}/week"]
    b.nodes.append(helper.make_node("Split", [x, split], columns, name=f"{name}/split", axis=1))

    def embed(column: str, offset: float, span: float) -> str:
        v = b.op("Add", [column, b.const(f"{column}/offset", np.float32(offset))], f"{column}_shift")
        v = b.op("Div", [v, b.const(f"{column}/span", np.float32(span))], f"{column}_unit")
        v = b.op("Mul", [two_pi, v], f"{column}_angle")
        v = b.op("Add", [v, phases], f"{column}_phase")
        v = b.op("Sin", [v], f"{column}_sin")
        return b.op("Mul", [sqrt2, v], f"{column}_embedding")

    lat = embed(columns[0], 90.0, 180.0)
    lon = embed(columns[1], 180.0, 360.0)
    week = embed(columns[2], -1.0, 48.0)
    after_start = b.op("Greater", [columns[2], b.const(f"{name}/zero", np.float32(0.0))], f"{name}/week_gt0")
    before_end = b.op("Less", [columns[2], b.const(f"{name}/max_week", np.float32(49.0))], f"{name}/week_lt49")
    mask = b.op(
        "Mul",
        [
            b.op("Cast", [after_start], f"{name}/week_gt0_f", to=TensorProto.FLOAT),
            b.op("Cast", [before_end], f"{name}/week_lt49_f", to=TensorProto.FLOAT),
        ],
        f"{name}/week_mask",
    )
    week = b.op("Mul", [week, mask], f"{name}/week_masked")
    return b.op("Concat", [lat, lon, week], name, (3 * k,), axis=1)


def _layer_weights(group: h5py.Group | None) -> dict[str, np.ndarray]:
    """A layer's weights keyed by short name (kernel, bias, gamma, ...)."""
    if group is None:
        return {}
    # h5py returns these as bytes or str depending on how the file was written
    paths = [w.decode() if isinstance(w, bytes) else w for w in group.attrs["weight_names"]]
    return {path.split("/")[-1].split(":")[0]: np.asarray(group[path]) for path in paths}


def build(h5_path: Path, output_layer: str) -> onnx.ModelProto:
    """Translate one Keras functional model into an ONNX graph returning output_layer."""
    with h5py.File(h5_path, "r") as f:
        config = json.loads(f.attrs["model_config"])["config"]
        weights_root = f["model_weights"]
        b = GraphBuilder()
        tensors: dict[str, str] = {}
        ((input_layer, *_),) = config["input_layers"]
        input_features = 0

        # Keras stores the layers in topological order, so every inbound tensor exists already
        for layer in config["layers"]:
            kind, cfg, name = layer["class_name"], layer["config"], layer["name"]
            if len(layer["inbound_nodes"]) > 1:
                raise ValueError(f"shared layers are not supported: {name}")
            inbound = [tensors[node[0]] for node in (layer["inbound_nodes"] or [[]])[0]]
            w = _layer_weights(weights_root.get(name))
            x = inbound[0] if inbound else ""
            shape = b.shapes.get(x, ())

            if kind == "InputLayer":
                input_features = cfg["batch_input_shape"][1]
                b.shapes[name] = (input_features,)
                out = name
            elif kind == "MelSpecLayerSimple":
                out = _mel_spec(b, cfg, w, x, shape[0])
            elif kind == "MDataLayer":
                out = _mdata(b, cfg, x)
            elif kind == "Dropout":
                out = x
            elif kind == "Activation":
                out = b.activation(x, cfg["activation"], name)
            elif kind == "Concatenate":
                if cfg["axis"] not in (-1, len(shape)):
                    raise ValueError(f"only channel concatenation is supported: {name}")
                # Channels are axis 1 in NCHW and the last axis of a rank-2 tensor alike
                channels = sum(b.shapes[t][0] for t in inbound)
                out = b.op("Concat", inbound, name, (channels, *shape[1:]), axis=1)
            elif kind in ("Add", "Multiply"):
                out = b.op(
                    "Add" if kind == "Add" else "Mul",
                    inbound,
                    name,
                    max((b.shapes[t] for t in inbound), key=lambda s: math.prod(s)),
                )
            elif kind == "BatchNormalization":
                if cfg["axis"] not in ([3], 3, -1, [-1]) or not (cfg["center"] and cfg["scale"]):
                    raise ValueError(f"unsupported batch norm config: {name}")
                params = [b.const(f"{name}/{p}", w[p]) for p in ("gamma", "beta", "moving_mean", "moving_variance")]
                out = b.op("BatchNormalization", [x, *params], name, shape, epsilon=cfg["epsilon"])
            elif kind in ("Conv2D", "DepthwiseConv2D"):
                c, h, wd = shape
                depthwise = kind == "DepthwiseConv2D"
                if cfg.get("groups", 1) != 1 or (depthwise and cfg["depth_multiplier"] != 1):
                    raise ValueError(f"unsupported conv config: {name}")
                kernel = w["depthwise_kernel" if depthwise else "kernel"]
                # Keras keeps (kh, kw, in, out) and depthwise (kh, kw, C, 1). ONNX wants (out, in / groups, kh, kw)
                kernel = kernel.transpose(2, 3, 0, 1) if depthwise else kernel.transpose(3, 2, 0, 1)
                out_h, out_w, pads = _spatial(cfg, h, wd, cfg["kernel_size"], cfg["strides"])
                inputs = [x, b.const(f"{name}/kernel", kernel)]
                if cfg["use_bias"]:
                    inputs.append(b.const(f"{name}/bias", w["bias"]))
                out = b.op(
                    "Conv",
                    inputs,
                    name if cfg["activation"] == "linear" else f"{name}/conv",
                    (kernel.shape[0], out_h, out_w),
                    kernel_shape=cfg["kernel_size"],
                    strides=cfg["strides"],
                    pads=pads,
                    group=c if depthwise else 1,
                )
                out = b.activation(out, cfg["activation"], name)
            elif kind in ("MaxPooling2D", "AveragePooling2D"):
                c, h, wd = shape
                out_h, out_w, pads = _spatial(cfg, h, wd, cfg["pool_size"], cfg["strides"])
                if any(pads):
                    raise ValueError(f"padded pooling is not supported: {name}")
                op_type = "MaxPool" if kind == "MaxPooling2D" else "AveragePool"
                out = b.op(op_type, [x], name, (c, out_h, out_w), kernel_shape=cfg["pool_size"], strides=cfg["strides"])
            elif kind == "ZeroPadding2D":
                (top, bottom), (left, right) = cfg["padding"]
                c, h, wd = shape
                pads = b.const(f"{name}/pads", np.array([0, 0, top, left, 0, 0, bottom, right], np.int64))
                out = b.op("Pad", [x, pads], name, (c, h + top + bottom, wd + left + right))
            elif kind == "GlobalAveragePooling2D":
                if cfg.get("keepdims", False):
                    raise ValueError(f"keepdims pooling is not supported: {name}")
                out = b.op("ReduceMean", [x], name, (shape[0],), axes=[2, 3], keepdims=0)
            elif kind == "Reshape":
                *spatial, channels = cfg["target_shape"]
                if spatial != [1, 1] or shape != (channels,):
                    raise ValueError(f"only (C,) to (1, 1, C) reshapes are supported: {name}")
                axes = b.const(f"{name}/axes", np.array([2, 3], np.int64))
                out = b.op("Unsqueeze", [x, axes], name, (channels, 1, 1))
            elif kind == "Dense":
                if len(shape) != 1:
                    raise ValueError(f"Dense on a rank-{len(shape) + 1} tensor is not supported: {name}")
                units = cfg["units"]
                out = b.op("MatMul", [x, b.const(f"{name}/kernel", w["kernel"])], f"{name}/matmul", (units,))
                if cfg["use_bias"]:
                    out = b.op("Add", [out, b.const(f"{name}/bias", w["bias"])], f"{name}/bias_add", (units,))
                out = b.activation(out, cfg["activation"], name)
            else:
                raise ValueError(f"unsupported layer {kind}: {name}")
            tensors[name] = out

        if output_layer not in tensors:
            raise ValueError(f"{h5_path.name} has no layer {output_layer}")
        model = b.model(input_layer, input_features, tensors[output_layer], config["name"])
    onnx.checker.check_model(model, full_check=True)
    return model


def convert(zip_path: Path, app_data: Path) -> None:
    """Write both models and their labels into the birdnet app data layout under app_data.

    Each model is saved to a temporary file and moved into place, so an
    interrupted conversion never leaves a truncated model at the loader's path.
    """
    with tempfile.TemporaryDirectory(prefix="birdnet_2_4_keras") as tmp, zipfile.ZipFile(zip_path) as zf:
        labels = {
            Path(member).name: zf.read(member)
            for member in zf.namelist()
            if member.startswith("labels/") and member.endswith(".txt")
        }
        for kind, (member, output_layer) in MODELS.items():
            h5_path = Path(zf.extract(member, tmp))
            logging.info("Building %s v2.4 from %s", kind, member)
            model = build(h5_path, output_layer)

            onnx_dir = app_data / f"{kind}-models" / "v2.4" / "onnx"
            onnx_dir.mkdir(parents=True, exist_ok=True)
            out_path = onnx_dir / "model-fp32.onnx"
            part = out_path.with_name(out_path.name + ".part")
            onnx.save(model, part)
            os.replace(part, out_path)
            # Left by the tf2onnx conversion that predates this module
            (onnx_dir / ".conversion.json").unlink(missing_ok=True)

            lang_dir = onnx_dir / "labels"
            shutil.rmtree(lang_dir, ignore_errors=True)
            lang_dir.mkdir()
            for file_name, data in labels.items():
                (lang_dir / file_name).write_bytes(data)
            logging.info("Wrote %s (%d locales)", out_path, len(labels))
