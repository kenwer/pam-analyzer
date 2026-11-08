"""The compiled app points birdnet at the models in its bundle."""

def test_compiled_app_points_the_model_path_into_its_bundle(monkeypatch) -> None:
    from pam_analyzer.app import __main__ as main_module

    # setenv first: delenv of an unset variable records nothing, so the value
    # the function sets would leak into every later test
    monkeypatch.setenv("BIRDNET_APP_DATA", "")
    monkeypatch.delenv("BIRDNET_APP_DATA")
    monkeypatch.setitem(main_module.__dict__, "__compiled__", object())
    main_module._configure_frozen_model_paths()
    assert main_module.os.environ["BIRDNET_APP_DATA"].endswith("birdnet-app-data")


def test_a_user_set_model_path_wins(monkeypatch) -> None:
    from pam_analyzer.app import __main__ as main_module

    monkeypatch.setenv("BIRDNET_APP_DATA", "/data/models")
    monkeypatch.setitem(main_module.__dict__, "__compiled__", object())
    main_module._configure_frozen_model_paths()
    assert main_module.os.environ["BIRDNET_APP_DATA"] == "/data/models"
