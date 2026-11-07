"""Background work for the in-app update: the release check and the download."""

import logging
import subprocess

from PySide6.QtCore import QObject, Signal, Slot

from ..infrastructure import update_feed, update_install
from ..infrastructure.update_feed import Update, UpdateError
from ..infrastructure.update_install import DownloadCancelled, InstallTarget

_log = logging.getLogger(__name__)


class UpdateCheckWorker(QObject):
    finished = Signal(object)  # Update or None
    failed = Signal(str)

    def __init__(self, feed_url: str, current_version: str, asset_name: str | None) -> None:
        super().__init__()
        self._feed_url = feed_url
        self._current_version = current_version
        self._asset_name = asset_name

    @Slot()
    def run(self) -> None:
        try:
            release = update_feed.fetch_latest_release(self._feed_url, self._current_version)
            self.finished.emit(update_feed.find_update(release, self._current_version, self._asset_name))
        except UpdateError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001  a raising slot would leave the check stuck
            _log.exception("update check crashed")
            self.failed.emit(str(exc))


class UpdateDownloadWorker(QObject):
    """Downloads, verifies and prepares an update, or reuses one staged earlier.

    reuse_only looks for a payload staged in an earlier session and never
    downloads. finished then carries None when there is none.
    """

    # object, not int: Qt's int is 32 bit and installers approach 2 GB
    progress = Signal(object, object)
    finished = Signal(object)  # the payload Path, or None
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, target: InstallTarget, update: Update, user_agent: str, *, reuse_only: bool = False) -> None:
        super().__init__()
        self._target = target
        self._update = update
        self._user_agent = user_agent
        self._reuse_only = reuse_only

    def _is_cancelled(self) -> bool:
        thread = self.thread()
        return thread is not None and thread.isInterruptionRequested()

    @Slot()
    def run(self) -> None:
        try:
            payload = update_install.fetch_payload(
                self._target,
                self._update,
                self._user_agent,
                self.progress.emit,
                self._is_cancelled,
                reuse_only=self._reuse_only,
            )
            self.finished.emit(payload)
        except DownloadCancelled:
            self.cancelled.emit()
        except (UpdateError, OSError, subprocess.CalledProcessError) as exc:
            _log.warning("update download failed: %s", exc)
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001  a raising slot would leave the download stuck
            _log.exception("update download crashed")
            self.failed.emit(str(exc))
