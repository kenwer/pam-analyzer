"""main() gives the window an update controller wired to the real feed."""

import pytest

from pam_analyzer.app.__main__ import attach_updater
from pam_analyzer.infrastructure.update_feed import DEFAULT_FEED_URL
from tests.conftest import stop_worker_threads
from tests.ui.test_main_window import _main_window


@pytest.fixture
def window(qtbot):
    win = _main_window()
    qtbot.addWidget(win)
    yield win
    stop_worker_threads(win)


def test_source_run_gets_a_controller_without_install_target(window, monkeypatch) -> None:
    monkeypatch.delenv("PAM_UPDATE_FEED_URL", raising=False)
    controller = attach_updater(window, compiled=False)
    assert controller._target is None
    assert controller._feed_url == DEFAULT_FEED_URL
    assert "Check for Updates..." in [a.text() for a in window.ui.menu_help.actions()]


def test_feed_url_can_be_overridden(window, monkeypatch) -> None:
    monkeypatch.setenv("PAM_UPDATE_FEED_URL", "http://localhost:8000/release.json")
    assert attach_updater(window, compiled=False)._feed_url == "http://localhost:8000/release.json"


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
