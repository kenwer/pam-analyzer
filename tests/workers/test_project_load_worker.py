"""Verify ProjectLoadWorker maps load_project_bundle's outcome to signals.

Runs worker.run() directly, no QThread: the worker's only job is the
try/except around load_project_bundle, so that's what's under test here.
The bundle's own composition logic is covered by
tests/infrastructure/test_project_loader.py.
"""

from pathlib import Path

import pam_analyzer.workers.project_load_worker as project_load_worker_module
from pam_analyzer.infrastructure import ProjectLoadResult
from pam_analyzer.workers.project_load_worker import ProjectLoadWorker


def test_run_emits_succeeded_with_bundle_result(monkeypatch, tmp_path: Path, qtbot) -> None:
    sentinel = ProjectLoadResult(
        project=object(), campaigns=[], audio_inventory=object(), analysis_result=None
    )
    monkeypatch.setattr(
        project_load_worker_module, "load_project_bundle", lambda *_args, **_kwargs: sentinel
    )
    worker = ProjectLoadWorker(project_repo=object(), campaign_repo=object(), folder=tmp_path)

    received: list[ProjectLoadResult] = []
    worker.succeeded.connect(received.append)
    worker.run()

    assert received == [sentinel]


def test_run_emits_failed_with_message_on_exception(monkeypatch, tmp_path: Path, qtbot) -> None:
    def _raise(*_args, **_kwargs):
        raise OSError("share unavailable")

    monkeypatch.setattr(project_load_worker_module, "load_project_bundle", _raise)
    worker = ProjectLoadWorker(project_repo=object(), campaign_repo=object(), folder=tmp_path)

    received: list[str] = []
    worker.failed.connect(received.append)
    worker.run()

    assert len(received) == 1
    assert tmp_path.name in received[0]
    assert "share unavailable" in received[0]
