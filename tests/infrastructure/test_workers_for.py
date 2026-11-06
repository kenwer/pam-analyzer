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


def _cores() -> int:
    return psutil.cpu_count(logical=False) or 1


@RUNNER_SPLITS
def test_split_fills_the_machine_without_oversubscribing(threads: int) -> None:
    """Workers times threads stays within the physical cores, and no whole worker is left out."""
    workers = workers_for(threads)

    assert workers >= 1
    assert workers * threads <= max(_cores(), threads)
    assert (workers + 1) * threads > _cores()


@RUNNER_SPLITS
def test_the_lib_gives_each_worker_the_chosen_threads(threads: int) -> None:
    """birdnet derives each session's threads from n_workers, and has to land on the runner's choice."""
    assert cpu_threads_per_worker(workers_for(threads)) == min(threads, _cores())
