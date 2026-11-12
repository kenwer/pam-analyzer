"""AppSettings keys that no panel test already covers."""


from pam_analyzer.ui.settings import AppSettings


def test_log_level_defaults_to_warning():
    assert AppSettings().log_level == "WARNING"


def test_a_stored_name_that_is_not_a_level_falls_back_to_warning():
    """A hand-edited or stale INI must not stop the app from starting."""
    AppSettings().log_level = "BANANA"
    assert AppSettings().log_level == "WARNING"


def test_update_check_at_startup_defaults_to_on():
    assert AppSettings().check_updates_at_startup is True
