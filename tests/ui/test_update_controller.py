"""UpdateController against fake feed and install functions on real worker threads."""

import threading
from pathlib import Path

import pytest

from pam_analyzer.infrastructure import update_feed, update_install
from pam_analyzer.infrastructure.update_feed import Update, UpdateError
from pam_analyzer.infrastructure.update_install import InstallTarget
from pam_analyzer.ui import toasts
from pam_analyzer.ui.update_controller import UpdateController, UpdateState
from tests.conftest import stop_worker_threads
from tests.ui.test_main_window import _main_window

UPDATE = Update("0.9.0", "notes", "https://example.org/r", "a.AppImage", "https://example.org/a", 10, "0" * 64)


class ToastLog:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.shown: list[tuple[str, str, str]] = []
        for kind in ("info", "error"):
            monkeypatch.setattr(
                toasts, f"show_{kind}_toast", lambda _parent, title, text, kind=kind, **_kw: self.shown.append((kind, title, text))
            )


@pytest.fixture
def toast_log(monkeypatch: pytest.MonkeyPatch) -> ToastLog:
    return ToastLog(monkeypatch)


@pytest.fixture
def window(qtbot):
    win = _main_window()
    qtbot.addWidget(win)
    yield win
    stop_worker_threads(win)


def _feed(monkeypatch: pytest.MonkeyPatch, update: Update | None = UPDATE, error: str | None = None) -> None:
    def fetch(url: str, version: str) -> dict:
        if error:
            raise UpdateError(error)
        return {}

    monkeypatch.setattr(update_feed, "fetch_latest_release", fetch)
    monkeypatch.setattr(update_feed, "find_update", lambda release, version, asset: update)


def _controller(window, target: InstallTarget | None) -> UpdateController:
    return UpdateController(window, "0.8.0", target, "https://feed", "a.AppImage")


def _target(tmp_path: Path) -> InstallTarget:
    return InstallTarget("linux", tmp_path / "PAM-Analyzer.AppImage", tmp_path / "stage")


def _menu_texts(window) -> list[str]:
    return [a.text() for a in window.ui.menu_help.actions()]


def test_check_for_updates_sits_in_the_help_menu(window) -> None:
    _controller(window, None)
    texts = _menu_texts(window)
    assert texts.index("Check for Updates...") < texts.index(window.ui.action_about.text())


def test_startup_check_failure_shows_nothing(qtbot, window, monkeypatch, toast_log) -> None:
    _feed(monkeypatch, error="offline")
    controller = _controller(window, None)
    controller.check(manual=False)
    qtbot.waitUntil(lambda: controller.state is UpdateState.FAILED)
    assert toast_log.shown == []
    assert not controller.button.isVisible()


def test_startup_check_with_update_shows_button_menu_text_and_toast(qtbot, window, monkeypatch, toast_log, tmp_path) -> None:
    _feed(monkeypatch)
    monkeypatch.setattr(update_install, "ready_payload", lambda target, update: None)
    window.show()
    controller = _controller(window, _target(tmp_path))
    controller.check(manual=False)
    qtbot.waitUntil(lambda: controller.state is UpdateState.AVAILABLE)
    assert controller.button.isVisible()
    assert controller.button.text() == "Update to 0.9.0"
    assert "Update to 0.9.0..." in _menu_texts(window)
    assert toast_log.shown == [("info", "Update available", "PAM Analyzer 0.9.0 is available")]


def test_manual_check_up_to_date_shows_toast(qtbot, window, monkeypatch, toast_log) -> None:
    _feed(monkeypatch, update=None)
    controller = _controller(window, None)
    controller.check(manual=True)
    qtbot.waitUntil(lambda: controller.state is UpdateState.IDLE and bool(toast_log.shown))
    assert toast_log.shown == [("info", "You're up to date", "PAM Analyzer 0.8.0 is the latest version")]


def test_manual_check_failure_shows_error(qtbot, window, monkeypatch, toast_log) -> None:
    _feed(monkeypatch, error="offline")
    controller = _controller(window, None)
    controller.check(manual=True)
    qtbot.waitUntil(lambda: controller.state is UpdateState.FAILED)
    assert [kind for kind, *_ in toast_log.shown] == ["error"]


def test_staged_payload_from_last_session_goes_straight_to_ready(qtbot, window, monkeypatch, toast_log, tmp_path) -> None:
    staged = tmp_path / "stage" / "0.9.0" / "a.AppImage"
    _feed(monkeypatch)
    monkeypatch.setattr(update_install, "ready_payload", lambda target, update: staged)
    window.show()
    controller = _controller(window, _target(tmp_path))
    controller.check(manual=False)
    qtbot.waitUntil(lambda: controller.state is UpdateState.READY)
    assert controller.button.text() == "Restart to update"


def test_staged_older_version_is_replaced_by_newer_release(qtbot, window, monkeypatch, toast_log, tmp_path) -> None:
    (tmp_path / "stage" / "0.8.5").mkdir(parents=True)
    _feed(monkeypatch)
    controller = _controller(window, _target(tmp_path))
    controller.check(manual=False)
    qtbot.waitUntil(lambda: controller.state is UpdateState.AVAILABLE and not (tmp_path / "stage" / "0.8.5").exists())
    assert controller.update.version == "0.9.0"


def test_block_reasons(window, tmp_path, monkeypatch) -> None:
    controller = _controller(window, None)
    assert controller.block_reason(UPDATE) == "This copy runs from source."
    mac = InstallTarget("macos", tmp_path / "PAM-Analyzer.app", tmp_path / "stage")
    linux = _target(tmp_path)
    monkeypatch.setattr(InstallTarget, "writable", lambda self: False)
    assert (
        _controller(window, mac).block_reason(UPDATE)
        == "Move PAM Analyzer into the Applications folder, then check again."
    )
    assert (
        _controller(window, linux).block_reason(UPDATE)
        == "Move the AppImage to a folder you can write to, then check again."
    )
    monkeypatch.setattr(InstallTarget, "writable", lambda self: True)
    no_digest = Update(**{**UPDATE.__dict__, "sha256": None})
    assert (
        _controller(window, linux).block_reason(no_digest)
        == "This release has no checksum, so it cannot be verified."
    )
    assert _controller(window, linux).block_reason(UPDATE) is None


def test_download_without_space_shows_error(window, monkeypatch, toast_log, tmp_path) -> None:
    monkeypatch.setattr(update_install, "free_space_ok", lambda target, update: False)
    controller = _controller(window, _target(tmp_path))
    controller._update = UPDATE
    controller.start_download()
    assert controller.state is not UpdateState.DOWNLOADING
    assert [kind for kind, *_ in toast_log.shown] == ["error"]


def test_download_reaches_ready(qtbot, window, monkeypatch, toast_log, tmp_path) -> None:
    payload = tmp_path / "stage" / "0.9.0" / "a.AppImage"
    monkeypatch.setattr(update_install, "free_space_ok", lambda target, update: True)
    monkeypatch.setattr(update_install, "ready_payload", lambda target, update: None)
    monkeypatch.setattr(update_install, "download", lambda u, dest, ua, progress, cancelled: progress(10, 10))
    monkeypatch.setattr(update_install, "prepare", lambda target, update, archive: payload)
    controller = _controller(window, _target(tmp_path))
    controller._update = UPDATE
    controller.start_download()
    qtbot.waitUntil(lambda: controller.state is UpdateState.READY)


def _ready(window, monkeypatch, tmp_path) -> tuple[UpdateController, list]:
    applied: list = []
    monkeypatch.setattr(update_install, "apply_update", lambda target, payload: applied.append(payload))
    controller = _controller(window, _target(tmp_path))
    controller._update = UPDATE
    controller._payload = tmp_path / "payload"
    controller._set_state(UpdateState.READY)
    return controller, applied


def test_restart_declined_keeps_app_open_and_applies_nothing(qtbot, window, monkeypatch, tmp_path) -> None:
    controller, applied = _ready(window, monkeypatch, tmp_path)
    window.show()
    monkeypatch.setattr(window, "_busy_labels", lambda: ["Species detection"])
    monkeypatch.setattr(window, "_confirm_stop", lambda busy, action, doing: False)
    controller.request_restart()
    assert window.isVisible()
    assert not controller.apply_on_exit
    controller.apply_pending_update()
    assert applied == []


def test_restart_accepted_applies_after_exit(qtbot, window, monkeypatch, tmp_path) -> None:
    controller, applied = _ready(window, monkeypatch, tmp_path)
    window.show()
    controller.request_restart()
    qtbot.waitUntil(lambda: window.closed)
    assert controller.apply_on_exit
    controller.apply_pending_update()
    assert applied == [tmp_path / "payload"]


def _on_ui_thread() -> bool:
    # A bool, never the QThread itself: tests/conftest.py's _stop_qt_threads
    # quits every running QThread it finds, the main thread's wrapper included
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication

    return QThread.currentThread() is QApplication.instance().thread()


def _thread_recording_toasts(monkeypatch) -> list[bool]:
    on_ui_thread: list[bool] = []
    for kind in ("info", "error"):
        monkeypatch.setattr(toasts, f"show_{kind}_toast", lambda *_a, **_kw: on_ui_thread.append(_on_ui_thread()))
    return on_ui_thread


def test_failed_download_is_handled_on_the_ui_thread(qtbot, window, monkeypatch, tmp_path) -> None:
    def mismatch(*_args) -> None:
        raise UpdateError("checksum mismatch")

    threads = _thread_recording_toasts(monkeypatch)
    monkeypatch.setattr(update_install, "free_space_ok", lambda target, update: True)
    monkeypatch.setattr(update_install, "ready_payload", lambda target, update: None)
    monkeypatch.setattr(update_install, "download", mismatch)
    controller = _controller(window, _target(tmp_path))
    controller._update = UPDATE
    controller.start_download()
    qtbot.waitUntil(lambda: bool(threads))
    assert threads == [True]
    qtbot.waitUntil(lambda: controller.state is UpdateState.AVAILABLE)


def test_cancelled_download_returns_to_available_on_the_ui_thread(qtbot, window, monkeypatch, tmp_path) -> None:
    from pam_analyzer.infrastructure.update_install import DownloadCancelled

    def cancelled(*_args) -> None:
        raise DownloadCancelled

    monkeypatch.setattr(update_install, "free_space_ok", lambda target, update: True)
    monkeypatch.setattr(update_install, "ready_payload", lambda target, update: None)
    monkeypatch.setattr(update_install, "download", cancelled)
    controller = _controller(window, _target(tmp_path))
    controller._update = UPDATE
    seen: list = []
    controller.stateChanged.connect(lambda state: seen.append((state, _on_ui_thread())))
    controller.start_download()
    qtbot.waitUntil(lambda: controller.state is UpdateState.AVAILABLE)
    assert seen[-1] == (UpdateState.AVAILABLE, True)


def test_a_blocked_swap_explains_itself_once(window, tmp_path, monkeypatch) -> None:
    from pam_analyzer.infrastructure.update_install import take_apply_failure

    monkeypatch.setattr(InstallTarget, "writable", lambda self: True)
    mac = InstallTarget("macos", tmp_path / "PAM-Analyzer.app", tmp_path / "stage")
    (tmp_path / "stage").mkdir()
    (tmp_path / "stage" / "apply-failed.txt").write_text("Operation not permitted")
    reason = _controller(window, mac).block_reason(UPDATE)
    assert reason is not None and "App Management" in reason
    assert take_apply_failure(mac) is None
    assert _controller(window, mac).block_reason(UPDATE) is None


def _slow_reuse(monkeypatch: pytest.MonkeyPatch, staged: Path | None) -> threading.Event:
    """The reuse check blocks until the returned event is set, then finds staged."""
    release = threading.Event()
    calls = {"n": 0}

    def slow_ready(target, update):
        calls["n"] += 1
        if calls["n"] == 1:
            release.wait(5)
            return staged
        return None

    monkeypatch.setattr(update_install, "ready_payload", slow_ready)
    monkeypatch.setattr(update_install, "verified_archive", lambda target, update: None)
    monkeypatch.setattr(update_install, "free_space_ok", lambda target, update: True)
    return release


def test_a_download_requested_during_the_reuse_check_waits_for_it(qtbot, window, monkeypatch, toast_log, tmp_path) -> None:
    """Two workers preparing the same staging folder would delete each other's files."""
    release = _slow_reuse(monkeypatch, None)
    started_after_reuse: list[bool] = []
    monkeypatch.setattr(
        update_install, "download", lambda *_a: started_after_reuse.append(release.is_set())
    )
    monkeypatch.setattr(update_install, "prepare", lambda target, update, archive: archive)
    _feed(monkeypatch)
    controller = _controller(window, _target(tmp_path))
    controller.check(manual=False)
    qtbot.waitUntil(lambda: controller.state is UpdateState.AVAILABLE)
    controller.start_download()
    assert controller.state is UpdateState.DOWNLOADING
    release.set()
    qtbot.waitUntil(lambda: controller.state is UpdateState.READY)
    assert started_after_reuse == [True]


def test_a_payload_found_by_the_reuse_check_ends_a_pending_download(qtbot, window, monkeypatch, toast_log, tmp_path) -> None:
    staged = tmp_path / "stage" / "0.9.0" / "a.AppImage"
    release = _slow_reuse(monkeypatch, staged)
    monkeypatch.setattr(update_install, "download", lambda *_a: pytest.fail("downloaded a staged update"))
    _feed(monkeypatch)
    controller = _controller(window, _target(tmp_path))
    controller.check(manual=False)
    qtbot.waitUntil(lambda: controller.state is UpdateState.AVAILABLE)
    controller.start_download()
    release.set()
    qtbot.waitUntil(lambda: controller.state is UpdateState.READY)
    assert controller._payload == staged


def test_cancel_during_the_reuse_check_skips_the_download(qtbot, window, monkeypatch, toast_log, tmp_path) -> None:
    release = _slow_reuse(monkeypatch, None)
    monkeypatch.setattr(update_install, "download", lambda *_a: pytest.fail("downloaded after cancel"))
    _feed(monkeypatch)
    controller = _controller(window, _target(tmp_path))
    controller.check(manual=False)
    qtbot.waitUntil(lambda: controller.state is UpdateState.AVAILABLE)
    controller.start_download()
    controller.cancel_download()
    assert controller.state is UpdateState.AVAILABLE
    release.set()
    qtbot.waitUntil(lambda: not controller._reusing)
    assert controller.state is UpdateState.AVAILABLE


def test_compiled_copy_without_target_gets_a_plain_reason(window) -> None:
    """A compiled build outside an AppImage or the installed folder is not a source run."""
    controller = UpdateController(window, "0.8.0", None, "https://feed", "a.AppImage", compiled=True)
    assert controller.block_reason(UPDATE) == (
        "This copy cannot update itself. Install the new version from the download page."
    )


def test_restart_waits_for_other_instances_on_windows(window, monkeypatch, toast_log, tmp_path) -> None:
    """With a second instance running, Setup would wait 60 s and quit silently."""
    from pam_analyzer.infrastructure import running_mutex

    applied: list = []
    monkeypatch.setattr(update_install, "apply_update", lambda target, payload: applied.append(payload))
    monkeypatch.setattr(running_mutex, "other_instance_running", lambda: True)
    controller = _controller(window, InstallTarget("windows", tmp_path / "PAM-Analyzer", tmp_path / "t"))
    controller._update = UPDATE
    controller._payload = tmp_path / "setup.exe"
    controller._set_state(UpdateState.READY)
    window.show()
    controller.request_restart()
    assert window.isVisible()
    assert not controller.apply_on_exit
    assert [kind for kind, *_ in toast_log.shown] == ["error"]
