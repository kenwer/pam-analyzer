"""Tests for the onnxruntime thread workaround in birdnet_onnx_threads.

Three things have to hold, and each of them fails silently: the patched loader
really pins the session's threads, the patch really reaches a freshly started
worker process, and the birdnet internals the module reaches for are still
where it expects them.

Silence is the point. A regression here costs about 3x throughput while every
other test still passes, every detection stays byte-identical and nothing is
logged, which is how the slowdown went unnoticed in the first place.

The onnxruntime session is faked throughout. What is under test is the options
handed to onnxruntime, and loading a real 73 MB model would say nothing more
about them.
"""

from __future__ import annotations

import pickle
import subprocess
import sys
import textwrap

import birdnet.core.backends as birdnet_backends
import pytest
from birdnet.core.backends import BackendLoader, OnnxBackend

from pam_analyzer.infrastructure import birdnet_onnx_threads as onnx_threads


class _FakeIO:
    name = "input"


class _FakeSession:
    """Stands in for ort.InferenceSession, recording how it was constructed."""

    last_kwargs: dict = {}

    def __init__(self, path, sess_options=None, providers=None, **kwargs) -> None:
        type(self).last_kwargs = {"path": path, "sess_options": sess_options, "providers": providers, **kwargs}

    def get_inputs(self):  # noqa: ANN202
        return [_FakeIO()]

    def get_outputs(self):  # noqa: ANN202
        return [_FakeIO()]

    def get_providers(self):  # noqa: ANN202
        return ["CPUExecutionProvider"]


class _FakeModel:
    """The part of a birdnet model this module touches."""

    def __init__(self, backend_type: type) -> None:
        self._backend_type = backend_type
        self._backend_kwargs: dict = {}

    @property
    def backend_type(self) -> type:
        return self._backend_type

    @property
    def backend_kwargs(self) -> dict:
        return self._backend_kwargs


@pytest.fixture(autouse=True)
def restore_thread_count():  # noqa: ANN201
    """Keep a test's chosen thread count from leaking into the next one."""
    before = onnx_threads._session_threads
    yield
    onnx_threads.apply(before)


@pytest.fixture
def fake_session(monkeypatch) -> type[_FakeSession]:
    import onnxruntime as ort

    monkeypatch.setattr(ort, "InferenceSession", _FakeSession)
    return _FakeSession


@pytest.mark.parametrize("threads", [1, 2])
def test_patched_loader_pins_session_threads(fake_session, tmp_path, threads: int) -> None:
    """The session gets the chosen pool size, not onnxruntime's one per core."""
    onnx_threads.apply(threads)
    onnx_threads._load_onnx_model_pinned(tmp_path / "model.onnx", "CPU")

    options = fake_session.last_kwargs["sess_options"]
    assert options is not None, "no SessionOptions, so onnxruntime sizes the pool per core"
    assert options.intra_op_num_threads == threads
    assert options.inter_op_num_threads == 1


def test_patched_loader_leaves_provider_selection_to_the_lib(fake_session, tmp_path) -> None:
    """The thread pools are the only difference from birdnet's own session."""
    onnx_threads._load_onnx_model_pinned(tmp_path / "model.onnx", "CPU")

    assert fake_session.last_kwargs["providers"] == ["CPUExecutionProvider"]


def test_apply_replaces_birdnets_loader() -> None:
    """The patch is installed where birdnet's ONNX backends look it up."""
    onnx_threads.apply()

    assert birdnet_backends.load_onnx_model is onnx_threads._load_onnx_model_pinned


def test_pin_session_threads_gives_the_model_a_carrier() -> None:
    """The carrier rides in backend_kwargs, which the pipeline hands to workers."""
    model = _FakeModel(OnnxBackend)

    assert onnx_threads.pin_session_threads(model) is model
    assert onnx_threads.CARRIER_KEY in model.backend_kwargs


def test_non_onnx_models_are_left_alone() -> None:
    """A TF-backed model is already single-threaded and needs no carrier."""

    class _TFBackend:
        pass

    model = _FakeModel(_TFBackend)
    onnx_threads.pin_session_threads(model)

    assert model.backend_kwargs == {}


def test_unpickling_a_worker_payload_patches_a_fresh_process() -> None:
    """The mechanism the whole design rests on, end to end.

    Workers start under 'spawn', so they import birdnet fresh and inherit
    nothing the parent patched. Unpickling is the only hook that travels. This
    pickles the lib's own BackendLoader, holding the backend_kwargs
    pin_session_threads() prepared, which is what the pipeline pickles into each
    worker, and checks that the child comes up patched.

    A subprocess is the only honest way to test it: anything in this process
    has the module imported and the patch applied already. The thread count is
    deliberately not the default, so a child that fell back to the default
    would fail rather than agree by accident.
    """
    model = _FakeModel(OnnxBackend)
    onnx_threads.pin_session_threads(model, threads=3)
    payload = pickle.dumps(
        BackendLoader(
            model_path=None,
            backend_type=OnnxBackend,
            backend_kwargs=model.backend_kwargs,
        )
    )

    script = textwrap.dedent("""
        import pickle, sys
        payload = sys.stdin.buffer.read()

        import birdnet.core.backends as backends
        before = backends.load_onnx_model

        pickle.loads(payload)

        from pam_analyzer.infrastructure import birdnet_onnx_threads
        patched = backends.load_onnx_model is not before
        print("patched" if patched else "unpatched", birdnet_onnx_threads._session_threads)
    """)
    result = subprocess.run([sys.executable, "-c", script], input=payload, capture_output=True)

    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.decode().strip() == "patched 3", (
        "unpickling a worker payload did not patch the child at the chosen thread "
        "count, so inference workers would not run the configuration they were given"
    )
