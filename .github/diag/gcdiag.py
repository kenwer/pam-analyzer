"""Temporary pytest plugin: is the cyclic garbage collector part of the Windows crash?

Loaded with `-p gcdiag` and steered by the GCDIAG environment variable:

    noop     load the plugin and do nothing, the control for the modes below
    log      report every collection that runs off the main thread
    disable  switch automatic collection off for the whole session
    collect  collect on the main thread before each test's fixtures are set up

Output goes to a private copy of stderr taken at import time. That survives a
crash that loses buffered text, and pytest's fd capture does not swallow it.
"""

import gc
import os
import threading

import pytest

MODE = os.environ.get("GCDIAG", "noop")
_MAIN = threading.get_ident()
_ERR = os.dup(2)
_counts = {"main": 0, "off-main": 0}


def _say(text: str) -> None:
    os.write(_ERR, f"gcdiag: {text}\n".encode())


def _on_gc(phase: str, info: dict) -> None:
    ident = threading.get_ident()
    if ident == _MAIN:
        if phase == "start":
            _counts["main"] += 1
        return
    if phase == "start":
        _counts["off-main"] += 1
    _say(f"{phase} gen={info['generation']} collected={info.get('collected')} on thread {ident:#x}")


def pytest_configure(config: pytest.Config) -> None:
    _say(f"mode={MODE} main thread {_MAIN:#x} thresholds {gc.get_threshold()}")
    if MODE == "log":
        gc.callbacks.append(_on_gc)
    elif MODE == "disable":
        gc.disable()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    if MODE == "log":
        _say(f"setup {item.name}")
    elif MODE == "collect":
        gc.collect()


def pytest_unconfigure(config: pytest.Config) -> None:
    if MODE == "log":
        _say(f"collections started: {_counts}")
