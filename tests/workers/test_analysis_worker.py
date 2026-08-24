"""AnalysisWorker runs a runner on the worker thread and reports one outcome.

The runner takes domain Campaign objects and loads each SpeciesFilter itself,
so the worker's only job is to run it off the UI thread and emit a single
finished(AnalysisRunResult). It handles the runner two ways, both of which
happen in production:
  * The runner returns an AnalysisRunResult (any status), forwarded verbatim.
    This is the normal path, including a per-campaign cancel or failure, where
    the runner's own loop stops and returns a partial outcome.
  * The runner raises, which the worker translates into a matching outcome.
    Setup before the runner's loop (_load_model, file counting) runs outside
    the runner's own try/except, so a model-load failure escapes run() and
    lands here rather than killing the worker thread.
"""

from pathlib import Path

from pam_analyzer.domain import (
    AnalysisRunResult,
    AnalysisSettings,
    Campaign,
    CampaignRunResult,
    CancelledError,
    FilterMode,
    Project,
    RunStatus,
)
from pam_analyzer.workers.analysis_worker import AnalysisWorker
from tests.conftest import CURRENT_MODEL_KEY


class FakeRunner:
    """Runner stub whose run() enacts `enacts`.

    enacts is either an AnalysisRunResult to return or an Exception to raise.
    Records the kwargs it was called with so a test can check forwarding.
    """

    def __init__(self, enacts: AnalysisRunResult | Exception) -> None:
        self.enacts = enacts
        self.calls: list[dict] = []

    def count_audio_files(self, _path: Path) -> int:
        return 0

    def available_locales(self) -> list[str]:
        return ["en", "de"]

    def run(self, **kwargs) -> AnalysisRunResult:
        self.calls.append(kwargs)
        if isinstance(self.enacts, Exception):
            raise self.enacts
        return self.enacts


def _worker(runner: FakeRunner, tmp_path: Path, *, preferred_lang: str = "en") -> AnalysisWorker:
    project = Project(folder=tmp_path, preferred_species_lang=preferred_lang)
    campaigns = [Campaign(name="X", folder=tmp_path / "X", species_filter_mode=FilterMode.LOCATION)]
    return AnalysisWorker(runner, project, campaigns, AnalysisSettings())


def test_success_emits_finished_and_forwards_preferred_lang(tmp_path: Path, qtbot) -> None:
    outcome = AnalysisRunResult(status=RunStatus.COMPLETED, elapsed=1.5)
    runner = FakeRunner(outcome)
    worker = _worker(runner, tmp_path, preferred_lang="de")

    with qtbot.waitSignal(worker.finished, raising=True) as blocker:
        worker.run()

    assert blocker.args == [outcome]
    # The worker's one piece of real forwarding: the project's preferred language.
    assert runner.calls[0]["preferred_lang"] == "de"


def test_forwards_returned_cancelled_outcome_with_its_campaigns(tmp_path: Path, qtbot) -> None:
    """Normal cancel path: the runner returns a CANCELLED outcome carrying the
    campaigns that finished before the cancel, and the worker forwards it
    unchanged, so the completed work survives the seam.
    """
    done = CampaignRunResult(
        campaign_name="a",
        detections_csv=tmp_path / f"detections-{CURRENT_MODEL_KEY}.csv",
        detection_count=3,
        wav_count=5,
        aru_count=1,
        elapsed=0.5,
    )
    outcome = AnalysisRunResult(status=RunStatus.CANCELLED, campaigns=(done,))
    worker = _worker(FakeRunner(outcome), tmp_path)

    with qtbot.waitSignal(worker.finished, raising=True) as blocker:
        worker.run()

    assert blocker.args == [outcome]  # forwarded verbatim, campaigns intact


def test_runner_that_raises_cancelled_is_translated(tmp_path: Path, qtbot) -> None:
    """Defensive path: the bundled runner returns a CANCELLED outcome rather
    than raising, so this guards the seam against a different runner (or the
    test fake) that signals cancellation by raising CancelledError.
    """
    worker = _worker(FakeRunner(CancelledError()), tmp_path)

    with qtbot.waitSignal(worker.finished, raising=True) as blocker:
        worker.run()

    assert blocker.args[0].status is RunStatus.CANCELLED


def test_exception_escaping_run_becomes_failed_outcome(tmp_path: Path, qtbot) -> None:
    """A failure that escapes run() uncaught (e.g. _load_model raising, which
    happens before the runner's own try/except) becomes a FAILED outcome with
    the message, instead of a dead worker thread and a stuck UI.
    """
    worker = _worker(FakeRunner(RuntimeError("model failed to load")), tmp_path)

    with qtbot.waitSignal(worker.finished, raising=True) as blocker:
        worker.run()

    outcome = blocker.args[0]
    assert outcome.status is RunStatus.FAILED
    assert "model failed to load" in (outcome.error or "")
