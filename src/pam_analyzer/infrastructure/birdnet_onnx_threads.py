"""Pins birdnet's onnxruntime sessions to one thread per inference worker.

This module is a workaround for a library default and nothing else. Delete it,
along with the `single_threaded()` call at each of the four load sites, once
birdnet builds its InferenceSession with SessionOptions of its own. Nothing
else in the app encodes the workaround: the runners load their models through
ordinary birdnet calls.

What it fixes: `birdnet.core.backends.load_onnx_model` constructs
InferenceSession without SessionOptions, so onnxruntime sizes the intra-op
thread pool at one thread per physical core. The prediction pipeline already
runs one worker process per physical core (`n_workers=None`), so the default
puts cores^2 threads on cores cores and the workers spend their time contending
for them rather than inferring. The lib pins its own TFLite and LiteRT
interpreters to `num_threads=1` for exactly this reason. Only the ONNX path was
left at the library default, which is why v2.4 was slower on ONNX than it had
been on litert. Measured over 243 one-minute files on 14 cores: 99 segments/s
before, 288 after, with byte-identical detections.

Why patching alone is not enough: the patch has to be in force inside the
worker processes, and those start with 'spawn' unless the application opts out,
so they import birdnet fresh and inherit nothing this module did in the parent.
The one thing that does travel is the pickled worker. `single_threaded()`
therefore drops a small carrier object into the model's backend_kwargs, which
the pipeline hands to each worker, and unpickling that carrier applies the
patch in whatever process it lands in. The backend ignores the extra kwarg,
because being unpickled is the carrier's whole job.

The birdnet internal this reaches for, `load_onnx_model`, is imported by name
at module import so that a library upgrade that moves it fails immediately and
visibly rather than silently costing 3x. Everything else here is public
library API. tests/infrastructure/test_birdnet_onnx_threads.py pins both.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import birdnet.core.backends as _backends
from birdnet.core.backends import OnnxBackend, _get_onnx_session_providers

# One thread per session, because the pipeline's parallelism is already one
# process per core. Raising this only makes sense together with a lower
# n_workers at the predict_session call sites.
SESSION_THREADS = 1

# Where the carrier rides in the model's backend_kwargs. Named rather than
# terse so that it reads as deliberate wherever the kwargs get dumped.
CARRIER_KEY = "pam_analyzer_single_threaded_sessions"

_applied = False


def _load_onnx_model_single_threaded(model_path: Path, device: str) -> Any:
    """Stand-in for birdnet's `load_onnx_model` that pins the session's threads.

    Provider selection is delegated to the lib, so a GPU device still resolves
    to whatever provider that build offers and the thread pools are the only
    difference from the session birdnet would have built.
    """
    import onnxruntime as ort

    providers, provider_options = _get_onnx_session_providers(device)

    options = ort.SessionOptions()
    options.intra_op_num_threads = SESSION_THREADS
    options.inter_op_num_threads = SESSION_THREADS

    try:
        return ort.InferenceSession(
            str(model_path.absolute()),
            sess_options=options,
            providers=providers,
            provider_options=provider_options,
        )
    except Exception as e:
        raise ValueError(
            f"Failed to load model '{model_path.absolute()}' using 'onnxruntime'. "
            "Ensure it is a valid ONNX model."
        ) from e


def apply() -> None:
    """Install the patch in this process. Idempotent."""
    global _applied
    if _applied:
        return
    _backends.load_onnx_model = _load_onnx_model_single_threaded
    _applied = True


def _unpickle_carrier() -> _Carrier:
    """Rebuild a carrier, patching the process it is rebuilt in.

    This runs in each inference worker, which is the point of the carrier
    existing at all. Pickle addresses this function by module and qualname, so
    reaching it imports this module in the worker first.
    """
    apply()
    return _Carrier()


class _Carrier:
    """Applies this module's patch wherever it is unpickled.

    Held in a model's backend_kwargs so it travels with the pickled worker into
    every inference process. It carries no data, and the backend constructor
    ignores the kwarg it arrives under.
    """

    def __reduce__(self) -> tuple[Any, tuple]:
        return (_unpickle_carrier, ())


def single_threaded(model: Any) -> Any:
    """Return `model` with its ONNX sessions pinned to one thread per worker.

    Applies to the model in place. Models on a non-ONNX backend are returned
    untouched, so this is safe to wrap around any load site.
    """
    apply()

    backend_type = model.backend_type
    if not (isinstance(backend_type, type) and issubclass(backend_type, OnnxBackend)):
        return model

    model.backend_kwargs[CARRIER_KEY] = _Carrier()
    return model
