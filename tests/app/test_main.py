"""The compiled app keeps its models in its own user data folder."""

from pam_analyzer.domain import paths


def test_compiled_app_points_the_model_path_at_its_user_data_folder(monkeypatch) -> None:
    from pam_analyzer.app import __main__ as main_module

    # setenv first: delenv of an unset variable records nothing, so the value
    # the function sets would leak into every later test
    monkeypatch.setenv("BIRDNET_APP_DATA", "")
    monkeypatch.delenv("BIRDNET_APP_DATA")
    monkeypatch.setitem(main_module.__dict__, "__compiled__", object())
    main_module._configure_frozen_model_dir()
    assert main_module.os.environ["BIRDNET_APP_DATA"] == str(paths.model_dir())


def test_a_user_set_model_path_wins(monkeypatch) -> None:
    from pam_analyzer.app import __main__ as main_module

    monkeypatch.setenv("BIRDNET_APP_DATA", "/data/models")
    monkeypatch.setitem(main_module.__dict__, "__compiled__", object())
    main_module._configure_frozen_model_dir()
    assert main_module.os.environ["BIRDNET_APP_DATA"] == "/data/models"


def test_model_dir_sits_beside_the_logs_not_in_the_birdnet_folder() -> None:
    # The Windows uninstaller deletes {localappdata}\PAM Analyzer\models by
    # this layout, and the log folder shares the parent
    assert paths.model_dir().name == "models"
    assert paths.model_dir().parent.name == "PAM Analyzer"
