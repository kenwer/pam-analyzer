"""AppSettings keys that no panel test already covers."""


from pam_analyzer.ui.settings import AppSettings


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
