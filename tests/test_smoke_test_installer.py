"""The installer smoke test accepts an app that keeps its models in the per-user folder."""

from pathlib import Path

from scripts import smoke_test_installer as smoke


def test_started_app_without_bundled_models_passes(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "pam-analyzer.log"
    log.write_text("2026-11-10 12:00:00 DEBUG BIRDNET_APP_DATA=<unset, per-user cache>\n", encoding="utf-8")
    monkeypatch.setattr(smoke, "log_file", lambda: log)
    monkeypatch.setattr(smoke, "STAY_ALIVE_S", 0)
    monkeypatch.setattr(smoke.time, "sleep", lambda _s: None)
    smoke.wait_until_started(0)
