"""Tests for onnx_telemetry, which keeps onnxruntime telemetry off in workers.

A regression fails silently: inference still works and every detection stays
the same, the workers just report telemetry again. The test that matters is the
subprocess one, because the parent process always has the module imported.
"""

from __future__ import annotations

import pickle
import subprocess
import sys
import textwrap

from birdnet.core.backends import BackendLoader, OnnxBackend

from pam_analyzer.infrastructure import onnx_telemetry


class _FakeModel:
    """The part of a birdnet model this module touches."""

    def __init__(self) -> None:
        self._backend_kwargs: dict = {}

    @property
    def backend_kwargs(self) -> dict:
        return self._backend_kwargs


def test_disable_in_workers_gives_the_model_a_carrier() -> None:
    """The carrier rides in backend_kwargs, which the pipeline hands to workers."""
    model = _FakeModel()

    assert onnx_telemetry.disable_in_workers(model) is model
    assert onnx_telemetry.CARRIER_KEY in model.backend_kwargs


def test_unpickling_a_worker_payload_disables_telemetry_in_a_fresh_process() -> None:
    """The mechanism end to end, in a child that starts without the module.

    Pickles the lib's own BackendLoader, which is what the pipeline pickles
    into each worker, and checks that unpickling it reaches
    ort.disable_telemetry_events in the child.
    """
    model = onnx_telemetry.disable_in_workers(_FakeModel())
    payload = pickle.dumps(
        BackendLoader(
            model_path=None,
            backend_type=OnnxBackend,
            backend_kwargs=model.backend_kwargs,
        )
    )

    script = textwrap.dedent("""
        import pickle, sys
        import onnxruntime as ort

        calls = []
        ort.disable_telemetry_events = lambda: calls.append(1)

        pickle.loads(sys.stdin.buffer.read())
        print("disabled" if calls else "enabled")
    """)
    result = subprocess.run([sys.executable, "-c", script], input=payload, capture_output=True)

    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.decode().strip() == "disabled"


def test_the_onnx_backend_accepts_the_carrier_kwarg(tmp_path) -> None:
    """The lib builds each backend with the kwargs, so the carrier must not break that."""

    class _Backend(OnnxBackend):
        prediction_out_idx = encoding_out_idx = probe_input_size_samples = None
        supports_encoding = precision = None

    model = onnx_telemetry.disable_in_workers(_FakeModel())

    _Backend(
        model_path=tmp_path / "model.onnx",
        device_name="CPU",
        half_precision=False,
        **model.backend_kwargs,
    )
