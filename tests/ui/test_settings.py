"""AppSettings keys that no panel test already covers."""

import pytest
from PySide6.QtCore import QSettings

from pam_analyzer.ui.settings import AppSettings


@pytest.fixture(autouse=True)
def _isolated_qsettings(tmp_path, monkeypatch):
    """Route AppSettings at a per-test INI file.

    Mirrors the fixture in tests/ui/test_campaigns_panel.py: the
    QSettings(organization, application) constructor is hardcoded to
    NativeFormat, so only replacing __init__ keeps the developer's real
    preferences untouched.
    """
    ini_path = tmp_path / "app_settings.ini"
    monkeypatch.setattr(
        AppSettings,
        "__init__",
        lambda self: setattr(self, "_settings", QSettings(str(ini_path), QSettings.Format.IniFormat)),
    )
    yield


def test_log_level_defaults_to_warning():
    assert AppSettings().log_level == "WARNING"


def test_log_level_survives_a_new_instance():
    AppSettings().log_level = "DEBUG"
    assert AppSettings().log_level == "DEBUG"


def test_log_level_can_be_changed_back():
    AppSettings().log_level = "DEBUG"
    AppSettings().log_level = "WARNING"
    assert AppSettings().log_level == "WARNING"


def test_a_stored_name_that_is_not_a_level_falls_back_to_warning():
    """A hand-edited or stale INI must not stop the app from starting."""
    AppSettings().log_level = "BANANA"
    assert AppSettings().log_level == "WARNING"
