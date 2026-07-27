"""AnalysisWorker maps a runner outcome to the right Qt signal.

The runner takes domain Campaign objects and loads each SpeciesFilter itself,
so the worker's only job is to run it on the worker thread and translate the
result, a CancelledError, or any other exception into succeeded/cancelled/failed.
"""

from pathlib import Path

from pam_analyzer.domain import (
    AnalysisRunResult,
    AnalysisSettings,
    Campaign,
    CancelledError,
    FilterMode,
    Project,
)
from pam_analyzer.workers.analysis_worker import AnalysisWorker


class FakeRunner:
    """Runner stub whose run() enacts `outcome`.

    outcome is either an AnalysisRunResult to return or an Exception to raise.
    Records the kwargs it was called with so a test can check forwarding.
    """

    def __init__(self, outcome: AnalysisRunResult | Exception) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    def count_audio_files(self, _path: Path) -> int:
        return 0

    def available_locales(self) -> list[str]:
        return ["en", "de"]

    def run(self, **kwargs) -> AnalysisRunResult:
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _worker(runner: FakeRunner, tmp_path: Path, *, preferred_lang: str = "en") -> AnalysisWorker:
    project = Project(folder=tmp_path, preferred_species_lang=preferred_lang)
    campaigns = [Campaign(name="X", folder=tmp_path / "X", species_filter_mode=FilterMode.LOCATION)]
    return AnalysisWorker(runner, project, campaigns, AnalysisSettings())


def test_success_emits_succeeded_and_forwards_preferred_lang(tmp_path: Path, qtbot) -> None:
    result = AnalysisRunResult(campaigns=(), elapsed=1.5)
    runner = FakeRunner(result)
    worker = _worker(runner, tmp_path, preferred_lang="de")

    with qtbot.waitSignal(worker.succeeded, raising=True) as blocker:
        worker.run()

    assert blocker.args == [result]
    # The worker's one piece of real forwarding: the project's preferred language.
    assert runner.calls[0]["preferred_lang"] == "de"


def test_cancelled_error_emits_cancelled_not_failed(tmp_path: Path, qtbot) -> None:
    worker = _worker(FakeRunner(CancelledError()), tmp_path)

    with qtbot.waitSignal(worker.cancelled, raising=True), qtbot.assertNotEmitted(worker.failed):
        worker.run()


def test_other_error_emits_failed_with_message(tmp_path: Path, qtbot) -> None:
    worker = _worker(FakeRunner(RuntimeError("model exploded")), tmp_path)

    with qtbot.waitSignal(worker.failed, raising=True) as blocker:
        worker.run()

    assert "model exploded" in blocker.args[0]
