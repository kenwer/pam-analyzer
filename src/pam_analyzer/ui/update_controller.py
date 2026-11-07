"""Drives the in-app update: check, offer, download, restart.

Owns a Help menu action and a status bar button. The update itself is
applied by main() after the event loop has ended, through
apply_pending_update(), so a declined quit confirmation applies nothing.
"""

import logging
from enum import Enum, auto
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal, SignalInstance, Slot
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import QMainWindow, QMessageBox, QToolButton

from ..infrastructure import running_mutex, update_install
from ..infrastructure.update_feed import Update
from ..infrastructure.update_install import InstallTarget
from ..workers import ReadOnlyThread, UpdateCheckWorker, UpdateDownloadWorker
from . import toasts
from .dialogs.update_dialog import UpdateDialog

_log = logging.getLogger(__name__)


class UpdateState(Enum):
    IDLE = auto()
    CHECKING = auto()
    AVAILABLE = auto()
    DOWNLOADING = auto()
    READY = auto()
    FAILED = auto()


class UpdateController(QObject):
    stateChanged = Signal(object)  # noqa: N815  Qt signal naming

    def __init__(
        self,
        window: QMainWindow,
        current_version: str,
        target: InstallTarget | None,
        feed_url: str,
        asset_name: str | None,
        *,
        compiled: bool = False,
    ) -> None:
        super().__init__(window)  # so MainWindow.running_threads() sees the worker threads
        self._window = window
        self._current_version = current_version
        self._target = target
        self._feed_url = feed_url
        self._asset_name = asset_name
        self._compiled = compiled
        self._state = UpdateState.IDLE
        self._update: Update | None = None
        self._payload: Path | None = None
        self._manual = False
        self._apply_on_exit = False
        # At most one download worker runs, so two never prepare the same staging folder
        self._download_thread: ReadOnlyThread | None = None
        self._reusing = False
        self._apply_failure = update_install.take_apply_failure(target) if target else None

        self._action = QAction("Check for Updates...", window)
        self._action.triggered.connect(self._activate)
        window.ui.menu_help.insertAction(window.ui.action_about, self._action)
        self.button = QToolButton()
        self.button.setAutoRaise(True)
        self.button.clicked.connect(self._activate)
        self.button.hide()
        window.ui.status_bar.addPermanentWidget(self.button)

    @property
    def state(self) -> UpdateState:
        return self._state

    @property
    def update(self) -> Update | None:
        return self._update

    @property
    def apply_on_exit(self) -> bool:
        return self._apply_on_exit

    def block_reason(self, update: Update) -> str | None:
        """Why the app cannot install update by itself, or None when it can."""
        if self._target is None:
            if self._compiled:
                return "This copy cannot update itself. Install the new version from the download page."
            return "This copy runs from source."
        if self._apply_failure is not None:
            if self._target.kind == "macos":
                return (
                    "macOS blocked the last update. Allow PAM Analyzer under System Settings > "
                    "Privacy & Security > App Management and check again, or install the new "
                    "version from the download page."
                )
            return "The last update could not replace the app. Install the new version from the download page."
        if not self._target.writable():
            if self._target.kind == "macos":
                return "Move PAM Analyzer into the Applications folder, then check again."
            return "Move the AppImage to a folder you can write to, then check again."
        if update.sha256 is None:
            return "This release has no checksum, so it cannot be verified."
        return None

    def check(self, manual: bool) -> None:
        if self._state in (UpdateState.CHECKING, UpdateState.DOWNLOADING):
            return
        self._manual = manual
        self._set_state(UpdateState.CHECKING)
        worker = UpdateCheckWorker(self._feed_url, self._current_version, self._asset_name)
        worker.finished.connect(self._on_checked)
        worker.failed.connect(self._on_check_failed)
        self._start(worker, worker.finished, worker.failed)

    def show_dialog(self) -> None:
        if self._update is None:
            return
        reason = self.block_reason(self._update)
        if not UpdateDialog(self._update, self._current_version, reason, self._window).exec():
            return
        if reason:
            QDesktopServices.openUrl(QUrl(self._update.page_url))
        else:
            self.start_download()

    def start_download(self) -> None:
        if self._update is None or self._target is None:
            return
        if not update_install.free_space_ok(self._target, self._update):
            size_gb = update_install.required_space(self._update) / 1e9
            toasts.show_error_toast(
                self._window, "Not enough disk space", f"The update needs about {size_gb:.1f} GB of free space."
            )
            return
        self._set_state(UpdateState.DOWNLOADING)
        if not self._reusing:  # otherwise _on_reuse_done starts it
            self._start_download_worker(reuse_only=False)

    def cancel_download(self) -> None:
        if self._state is not UpdateState.DOWNLOADING:
            return
        if self._reusing:  # the download has not started yet
            self._set_state(UpdateState.AVAILABLE)
        elif self._download_thread is not None:
            self._download_thread.requestInterruption()

    def request_restart(self) -> None:
        if self._state is not UpdateState.READY:
            return
        if self._target is not None and self._target.kind == "windows" and running_mutex.other_instance_running():
            # Setup would wait for that instance, give up after 60 s and leave no trace
            toasts.show_error_toast(
                self._window,
                "PAM Analyzer is open twice",
                "Close the other PAM Analyzer window, then select Restart to update again.",
            )
            return
        self._apply_on_exit = True
        self._window.close()
        if self._window.isVisible():  # the user kept the running work
            self._apply_on_exit = False

    def apply_pending_update(self) -> None:
        if self._apply_on_exit and self._target is not None and self._payload is not None:
            update_install.apply_update(self._target, self._payload)

    def _start(self, worker: QObject, *done: SignalInstance) -> ReadOnlyThread:
        thread = ReadOnlyThread(self)
        # The parented thread keeps the worker's Python wrapper alive. Without a
        # reference the worker is freed while its thread still runs it.
        thread.worker = worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        for signal in done:
            signal.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()
        return thread

    def _start_download_worker(self, *, reuse_only: bool) -> None:
        worker = UpdateDownloadWorker(
            self._target, self._update, f"PAM-Analyzer/{self._current_version}", reuse_only=reuse_only
        )
        # Bound methods, never lambdas: Qt queues a connection to a QObject's
        # method onto that object's thread, a lambda runs on the worker thread
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_reuse_done if reuse_only else self._on_downloaded)
        worker.failed.connect(self._on_reuse_failed if reuse_only else self._on_download_failed)
        worker.cancelled.connect(self._on_download_cancelled)
        self._reusing = reuse_only
        self._download_thread = self._start(worker, worker.finished, worker.failed, worker.cancelled)

    @Slot(object)
    def _on_checked(self, update: Update | None) -> None:
        if update is None:
            self._set_state(UpdateState.IDLE)
            if self._manual:
                toasts.show_info_toast(
                    self._window, "You're up to date", f"PAM Analyzer {self._current_version} is the latest version"
                )
            return
        self._update = update
        self._set_state(UpdateState.AVAILABLE)
        if not self._manual:
            toasts.show_info_toast(self._window, "Update available", f"PAM Analyzer {update.version} is available")
        else:
            self.show_dialog()
        if self._state is UpdateState.AVAILABLE and self.block_reason(update) is None:
            self._start_download_worker(reuse_only=True)

    @Slot(str)
    def _on_check_failed(self, message: str) -> None:
        _log.info("update check failed: %s", message)
        self._set_state(UpdateState.FAILED)
        if self._manual:
            toasts.show_error_toast(self._window, "Update check failed", message)

    @Slot(object, object)
    def _on_progress(self, done: int, total: int) -> None:
        if self._state is UpdateState.DOWNLOADING and total:
            self.button.setText(f"Downloading {self._update.version}: {100 * done // total}%")

    @Slot(object)
    def _on_downloaded(self, payload: Path) -> None:
        self._payload = payload
        self._set_state(UpdateState.READY)

    @Slot(object)
    def _on_reuse_done(self, payload: Path | None) -> None:
        self._reusing = False
        if payload is not None:
            self._on_downloaded(payload)
        elif self._state is UpdateState.DOWNLOADING:  # the user asked for it meanwhile
            self._start_download_worker(reuse_only=False)

    @Slot(str)
    def _on_reuse_failed(self, message: str) -> None:
        _log.info("no reusable staged update: %s", message)
        self._on_reuse_done(None)

    @Slot(str)
    def _on_download_failed(self, message: str) -> None:
        self._set_state(UpdateState.AVAILABLE)
        toasts.show_error_toast(self._window, "Update download failed", message)

    @Slot()
    def _on_download_cancelled(self) -> None:
        self._set_state(UpdateState.AVAILABLE)

    def _activate(self) -> None:
        """The menu action or the status bar button was clicked."""
        if self._state is UpdateState.AVAILABLE:
            self.show_dialog()
        elif self._state is UpdateState.READY:
            self.request_restart()
        elif self._state is UpdateState.DOWNLOADING:
            answer = QMessageBox.question(self._window, "Cancel update?", "Stop downloading the update?")
            if answer == QMessageBox.StandardButton.Yes:
                self.cancel_download()
        else:
            self.check(manual=True)

    def _set_state(self, state: UpdateState) -> None:
        self._state = state
        version = self._update.version if self._update else ""
        texts = {
            UpdateState.AVAILABLE: (f"Update to {version}", f"Update to {version}..."),
            UpdateState.DOWNLOADING: (f"Downloading {version}...", "Downloading Update..."),
            UpdateState.READY: ("Restart to update", "Restart to Update"),
        }
        button_text, action_text = texts.get(state, ("", "Check for Updates..."))
        self.button.setText(button_text)
        self.button.setVisible(bool(button_text))
        self._action.setText(action_text)
        self._action.setEnabled(state not in (UpdateState.CHECKING, UpdateState.DOWNLOADING))
        self.stateChanged.emit(state)
