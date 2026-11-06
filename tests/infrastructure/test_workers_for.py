"""Tests for workers_for, the worker split the v2.4 and Perch runners share.

Both properties fail silently: every detection stays the same and only
throughput or memory moves.
"""

from __future__ import annotations

import psutil
import pytest
from birdnet.core.backends import cpu_threads_per_worker

from pam_analyzer.infrastructure import birdnet_2_4_runner, perch_runner
from pam_analyzer.infrastructure._analysis_helpers import workers_for

RUNNER_SPLITS = pytest.mark.parametrize(
    "threads",
    [birdnet_2_4_runner.THREADS_PER_WORKER, perch_runner.THREADS_PER_WORKER],
    ids=["v2.4", "perch"],
)

# Odd counts matter: CI runners have shipped with 3 physical cores.
CORE_COUNTS = pytest.mark.parametrize("cores", [1, 2, 3, 4, 7, 8, 14], indirect=True)


@pytest.fixture
def cores(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> int:
    count: int = request.param
    monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: count)
    return count


@RUNNER_SPLITS
@CORE_COUNTS
def test_split_fills_the_machine_without_oversubscribing(threads: int, cores: int) -> None:
    """Workers times threads stays within the physical cores, and no whole worker is left out."""
    workers = workers_for(threads)

    assert workers >= 1
    assert workers * threads <= max(cores, threads)
    assert (workers + 1) * threads > cores


@RUNNER_SPLITS
@CORE_COUNTS
def test_the_lib_gives_each_worker_at_least_the_chosen_threads(threads: int, cores: int) -> None:
    """birdnet derives each session's threads from n_workers.

    Each worker gets at least the runner's choice. Leftover cores go to the
    workers, so on 3 cores a single worker runs 3 threads, never oversubscribing.
    """
    workers = workers_for(threads)
    lib_threads = cpu_threads_per_worker(workers)

    assert lib_threads >= min(threads, cores)
    assert workers * lib_threads <= cores
