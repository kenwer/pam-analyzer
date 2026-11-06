"""Keeps onnxruntime telemetry off inside birdnet's inference workers.

On Windows onnxruntime emits telemetry events unless the process calls
`ort.disable_telemetry_events()`, and that call only covers the process it runs
in. Other platforms are covered by the environment variable app/__main__.py
sets, which child processes inherit.

The pipeline starts its workers with 'spawn', so they import everything fresh
and inherit no call the parent made. The one thing that does travel is the
pickled worker payload, which carries the model's backend_kwargs.
`disable_in_workers()` therefore drops a small carrier object into those
kwargs, and unpickling the carrier disables telemetry in whatever process it
lands in. The backend constructor accepts and ignores the extra kwarg.
"""

from __future__ import annotations

from typing import Any

# Named rather than terse so that it reads as deliberate wherever the kwargs
# get dumped.
CARRIER_KEY = "pam_analyzer_onnx_telemetry_off"


def disable() -> None:
    """Turn onnxruntime telemetry off in this process."""
    import onnxruntime as ort

    ort.disable_telemetry_events()


def _unpickle_carrier() -> _Carrier:
    """Rebuild a carrier, disabling telemetry in the process it is rebuilt in.

    Pickle addresses this function by module and qualname, so reaching it
    imports this module in the worker first.
    """
    disable()
    return _Carrier()


class _Carrier:
    """Disables onnxruntime telemetry wherever it is unpickled."""

    def __reduce__(self) -> tuple[Any, tuple]:
        return (_unpickle_carrier, ())


def disable_in_workers(model: Any) -> Any:
    """Return `model` with telemetry disabled here and in every inference worker.

    Applies to the model in place.
    """
    disable()
    model.backend_kwargs[CARRIER_KEY] = _Carrier()
    return model
