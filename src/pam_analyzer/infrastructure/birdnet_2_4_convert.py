"""BirdNET v2.4's model package: upstream's Keras zip, converted to ONNX on this machine.

Upstream publishes no ONNX export of v2.4, and the app does not redistribute
converted weights. So the package downloads BirdNET_v2.4_keras.zip from Zenodo
like any other artifact, and its prepare step converts it with
birdnet_2_4_keras_onnx and then deletes it.

Once converted, the package lists no artifacts. Each model folder then holds a
marker naming the zip and CONVERSION_REVISION it was built from. The models
themselves are not hashed, because their bytes may differ by CPU.

This module stays free of onnx and h5py so the model status panel can ask
what is missing without importing them.
"""

import logging
from pathlib import Path

from birdnet.utils.local_data import get_birdnet_app_data_folder

from .model_store import Artifact, marker_path

SOURCE_URL = "https://zenodo.org/records/15050749/files/BirdNET_v2.4_keras.zip?download=1"
SOURCE_SIZE = 123_874_754
# Zenodo publishes md5 c59eb166ee53c8e3973ef094729c7886 for the same file
SOURCE_SHA256 = "893469ec2780d74f1f2bed5957071c97db26e75c97d9f55e6f664973b99e88ce"

# Bump when a change to birdnet_2_4_keras_onnx changes the models, so existing
# installs convert again
CONVERSION_REVISION = 1

_KINDS = ("acoustic", "geo")
_MARKER_TEXT = f"{SOURCE_SHA256} r{CONVERSION_REVISION}\n"


def source_artifact() -> Artifact:
    return Artifact(
        id="birdnet-2.4-keras",
        url=SOURCE_URL,
        size=SOURCE_SIZE,
        sha256=SOURCE_SHA256,
        dest=get_birdnet_app_data_folder() / "downloads" / "BirdNET_v2.4_keras.zip",
    )


def _onnx_dir(kind: str) -> Path:
    return get_birdnet_app_data_folder() / f"{kind}-models" / "v2.4" / "onnx"


def _conversion_marker(kind: str) -> Path:
    return _onnx_dir(kind) / "conversion.txt"


def converted() -> bool:
    """Whether both models and their labels are in place from the current zip and revision."""
    for kind in _KINDS:
        folder = _onnx_dir(kind)
        if not (folder / "model-fp32.onnx").is_file() or not (folder / "labels").is_dir():
            return False
        try:
            if _conversion_marker(kind).read_text(encoding="ascii") != _MARKER_TEXT:
                return False
        except (OSError, UnicodeDecodeError):
            return False
    return True


def artifacts() -> tuple[Artifact, ...]:
    return () if converted() else (source_artifact(),)


def prepare() -> None:
    """Convert the downloaded zip, then delete it. Returns at once when already converted.

    The markers go first and come back last, so a conversion that fails
    halfway never counts as done. The zip stays until the end, so a retry
    converts again without downloading.
    """
    if converted():
        return
    from .birdnet_2_4_keras_onnx import convert

    for kind in _KINDS:
        _conversion_marker(kind).unlink(missing_ok=True)
    source = source_artifact()
    logging.info("Converting BirdNET v2.4 from %s", source.dest)
    convert(source.dest, get_birdnet_app_data_folder())
    for kind in _KINDS:
        _conversion_marker(kind).write_text(_MARKER_TEXT, encoding="ascii")
    source.dest.unlink(missing_ok=True)
    marker_path(source).unlink(missing_ok=True)
