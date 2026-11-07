"""The update workers map the feed and install functions to signals.

Runs worker.run() directly, no QThread, like test_project_load_worker.py.
"""

from pathlib import Path

import pytest

from pam_analyzer.infrastructure import update_feed, update_install
from pam_analyzer.infrastructure.update_feed import Update, UpdateError
from pam_analyzer.infrastructure.update_install import DownloadCancelled, InstallTarget
from pam_analyzer.workers.update_workers import UpdateCheckWorker, UpdateDownloadWorker

UPDATE = Update("0.9.0", "notes", "https://example.org/r", "a.AppImage", "https://example.org/a", 10, "0" * 64)


def _target(tmp_path: Path) -> InstallTarget:
    return InstallTarget("linux", tmp_path / "PAM-Analyzer.AppImage", tmp_path / "stage")


def test_check_worker_emits_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_feed, "fetch_latest_release", lambda url, version: {"url": url})
    monkeypatch.setattr(update_feed, "find_update", lambda release, version, asset: UPDATE)
    worker = UpdateCheckWorker("https://feed", "0.8.0", "a.AppImage")
    received: list = []
    worker.finished.connect(received.append)
    worker.run()
    assert received == [UPDATE]


def test_check_worker_emits_failed_on_update_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def offline(url: str, version: str) -> dict:
        raise UpdateError("offline")

    monkeypatch.setattr(update_feed, "fetch_latest_release", offline)
    worker = UpdateCheckWorker("https://feed", "0.8.0", "a.AppImage")
    failed: list[str] = []
    worker.failed.connect(failed.append)
    worker.run()
    assert failed == ["offline"]


def test_download_worker_reports_progress_and_the_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = tmp_path / "stage" / "0.9.0" / "a.AppImage"

    def fake_fetch(target, update, user_agent, progress, cancelled, *, reuse_only):
        assert not reuse_only
        progress(5, 10)
        progress(10, 10)
        return payload

    monkeypatch.setattr(update_install, "fetch_payload", fake_fetch)
    worker = UpdateDownloadWorker(_target(tmp_path), UPDATE, "UA")
    progress: list = []
    received: list = []
    worker.progress.connect(lambda done, total: progress.append((done, total)))
    worker.finished.connect(received.append)
    worker.run()
    assert progress == [(5, 10), (10, 10)]
    assert received == [payload]


def test_download_worker_emits_cancelled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def cancelled_fetch(*_args, **_kw) -> None:
        raise DownloadCancelled

    monkeypatch.setattr(update_install, "fetch_payload", cancelled_fetch)
    worker = UpdateDownloadWorker(_target(tmp_path), UPDATE, "UA")
    cancelled: list = []
    worker.cancelled.connect(lambda: cancelled.append(True))
    worker.run()
    assert cancelled == [True]


def test_download_worker_emits_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def mismatch(*_args, **_kw) -> None:
        raise UpdateError("checksum mismatch")

    monkeypatch.setattr(update_install, "fetch_payload", mismatch)
    worker = UpdateDownloadWorker(_target(tmp_path), UPDATE, "UA")
    failed: list[str] = []
    worker.failed.connect(failed.append)
    worker.run()
    assert failed == ["checksum mismatch"]


def test_check_worker_reports_unexpected_errors_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(release, version, asset):
        raise RuntimeError("bug in the parser")

    monkeypatch.setattr(update_feed, "fetch_latest_release", lambda url, version: {})
    monkeypatch.setattr(update_feed, "find_update", broken)
    worker = UpdateCheckWorker("https://feed", "0.8.0", "a.AppImage")
    failed: list[str] = []
    worker.failed.connect(failed.append)
    worker.run()
    assert failed == ["bug in the parser"]
