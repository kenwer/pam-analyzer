"""The app's model key must name the model the library actually downloads.

MODEL_KEY becomes a CSV filename and the per-row Model column, so it is the
only record of which model produced a given detection. If a birdnet upgrade
swaps the underlying weights (most likely when BirdNET v3.0 leaves preview)
and the key does not move with it, two different models' detections end up
indistinguishable in one file. These tests fail that upgrade loudly instead.
"""

from __future__ import annotations

from birdnet.acoustic.models.v3_0 import onnx as v3_onnx

from pam_analyzer.infrastructure.birdnet_runner import (
    MODEL_KEY,
    MODEL_PRECISION,
    BirdnetRunner,
)

# e.g. "BirdNET+_V3.0-preview3.1_Global_11K_FP32.onnx"
SHIPPED_MODEL_FILE = v3_onnx.models[MODEL_PRECISION].dl_file_name


def test_model_key_names_the_shipped_release():
    """The version suffix of MODEL_KEY is the release token in the model file.

    When this fails, birdnet has changed the model file. Re-label MODEL_KEY in
    birdnet_runner.py and note in the changelog that new runs write to a
    different CSV. This also covers preview status: while the library ships a
    preview build, a key that drops "preview" stops matching, so detections
    from a preview cannot be mistaken for a release run.
    """
    version = MODEL_KEY.removeprefix("BirdNET-")
    assert f"_V{version}_" in SHIPPED_MODEL_FILE, (
        f"birdnet now ships {SHIPPED_MODEL_FILE!r}, which is not the release "
        f"that {MODEL_KEY!r} names."
    )


def test_model_key_is_filesystem_safe():
    """It is used verbatim as a filename component."""
    assert MODEL_KEY == MODEL_KEY.strip()
    assert not set(MODEL_KEY) & set('/\\:*?"<>|')


def test_runner_exposes_the_model_key():
    assert BirdnetRunner().model_key == MODEL_KEY
