"""Argument handling in scripts/build.py that needs no compiler."""

import pytest

from scripts import build


@pytest.mark.parametrize("stage", ["app", "installer"])
def test_partial_stages_are_windows_only(monkeypatch, capsys, stage: str) -> None:
    monkeypatch.setattr(build.sys, "platform", "darwin")
    with pytest.raises(SystemExit):
        build._parse_args(["--stage", stage])
    assert "only supported on Windows" in capsys.readouterr().err


@pytest.mark.parametrize("stage", ["app", "installer"])
def test_partial_stages_are_accepted_on_windows(monkeypatch, stage: str) -> None:
    monkeypatch.setattr(build.sys, "platform", "win32")
    assert build._parse_args(["--stage", stage]).stage == stage
