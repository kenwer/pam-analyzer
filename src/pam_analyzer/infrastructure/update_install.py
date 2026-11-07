"""Where the app is installed, and staging, downloading and applying an update.

macOS and Linux stage next to the app, so applying is a rename on one volume.
Windows stages in %TEMP%, since the installer copies the files anyway.
"""

import hashlib
import http.client
import logging
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from packaging.version import InvalidVersion, Version

from .update_feed import Update, UpdateError

_log = logging.getLogger(__name__)

STAGING_DIR_NAME = ".pam-analyzer-update"
# Written when apply_update could not swap the app, read by the next session
_APPLY_FAILED = "apply-failed.txt"
# An update needs room for the download, an extracted copy and the old version
_FREE_SPACE_FACTOR = 2.5


@dataclass(frozen=True)
class InstallTarget:
    kind: Literal["windows", "macos", "linux"]
    # The install folder on Windows, the .app on macOS, the AppImage file on Linux
    app_path: Path
    staging_dir: Path

    @property
    def old_path(self) -> Path:
        """Where apply_update moves the running app aside."""
        return self.app_path.with_name(self.app_path.name + ".old")

    def version_dir(self, update: Update) -> Path:
        return self.staging_dir / update.version

    def archive_path(self, update: Update) -> Path:
        return self.version_dir(update) / update.asset_name

    def writable(self) -> bool:
        folder = self.app_path if self.kind == "windows" else self.app_path.parent
        return os.access(folder, os.W_OK)


def detect_install_target(system: str, executable: Path, environ: Mapping[str, str]) -> InstallTarget | None:
    """The installed app around executable, or None where an in-app install cannot work."""
    if system == "win32":
        # Setup installs into its own folder, so only a copy it installed can be updated
        if not (executable.parent / "unins000.exe").is_file():
            return None
        temp = environ.get("TEMP") or tempfile.gettempdir()
        return InstallTarget("windows", executable.parent, Path(temp) / "pam-analyzer-update")
    if system == "darwin":
        app = next((p for p in executable.parents if p.suffix == ".app"), None)
        if app is None:
            return None
        return InstallTarget("macos", app, app.parent / STAGING_DIR_NAME)
    appimage = environ.get("APPIMAGE")
    if not appimage:
        return None
    path = Path(appimage)
    return InstallTarget("linux", path, path.parent / STAGING_DIR_NAME)


def required_space(update: Update) -> int:
    return int(_FREE_SPACE_FACTOR * update.size)


def free_space_ok(target: InstallTarget, update: Update) -> bool:
    folder = target.staging_dir if target.staging_dir.exists() else target.staging_dir.parent
    return shutil.disk_usage(folder).free >= required_space(update)


def _remove(path: Path) -> None:
    """Delete a file or a folder tree. What cannot be removed is logged."""

    def warn(_fn: object, failed: str | Path, exc: BaseException) -> None:
        _log.warning("could not remove %s: %s", failed, exc)

    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, onexc=warn)
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        warn(None, path, exc)


def _prune_staged(target: InstallTarget, keep: Callable[[str], bool]) -> None:
    if not target.staging_dir.is_dir():
        return
    for staged in target.staging_dir.iterdir():
        if staged.is_dir() and not keep(staged.name):
            _log.info("removing staged update %s", staged)
            _remove(staged)


def cleanup_after_update(target: InstallTarget | None, current_version: str) -> None:
    """Delete staged versions up to current_version and the app's .old copy.

    Runs at startup. A staged version newer than the running one is kept, so a
    downloaded update survives a normal quit. Errors are logged, not raised.
    """
    if target is None:
        return
    _remove(target.old_path)
    try:
        running = Version(current_version)
    except InvalidVersion:
        return

    def newer(name: str) -> bool:
        try:
            return Version(name) > running
        except InvalidVersion:
            return True

    _prune_staged(target, keep=newer)


class DownloadCancelled(Exception):
    """The user cancelled. The partial file stays, so the next download resumes."""


_CHUNK = 1 << 20
_APP_BUNDLE = "PAM-Analyzer.app"


def download(
    update: Update,
    dest: Path,
    user_agent: str,
    progress: Callable[[int, int], None],
    cancelled: Callable[[], bool],
) -> None:
    """Download update to dest, resuming a partial file, and verify its sha256.

    Raises UpdateError on a missing digest, a network failure or a mismatch,
    and DownloadCancelled when cancelled() turns true.
    """
    if update.sha256 is None:
        raise UpdateError("This release has no checksum, so it cannot be verified.")
    part = dest.with_name(dest.name + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    have = part.stat().st_size if part.exists() else 0
    if update.size and have >= update.size:
        # Complete already, say killed between the last write and the rename.
        # A Range request past the end would only get a 416.
        _finish(part, dest, _sha256_of(part), update.sha256)
        return
    headers = {"User-Agent": user_agent}
    if have:
        headers["Range"] = f"bytes={have}-"
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(urllib.request.Request(update.asset_url, headers=headers), timeout=30) as response:
            if response.status == 206:
                with part.open("rb") as existing:
                    for block in iter(lambda: existing.read(_CHUNK), b""):
                        digest.update(block)
                mode = "ab"
            else:
                have, mode = 0, "wb"
            expected_end = have + int(response.headers.get("Content-Length", 0))
            total = update.size or expected_end
            with part.open(mode) as out:
                while block := response.read(_CHUNK):
                    out.write(block)
                    digest.update(block)
                    have += len(block)
                    progress(have, total)
                    if cancelled():
                        raise DownloadCancelled
            # read(n) returns b"" on a dropped connection instead of raising
            if have < expected_end:
                raise UpdateError("The download was interrupted. It resumes on the next try.")
    except urllib.error.HTTPError as exc:
        if exc.code == 416:  # the .part does not fit the file on the server
            part.unlink(missing_ok=True)
        raise UpdateError(f"The download failed: {exc}") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise UpdateError(f"The download failed: {exc}") from exc
    _finish(part, dest, digest.hexdigest(), update.sha256)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _finish(part: Path, dest: Path, actual: str, expected: str) -> None:
    if actual != expected:
        part.unlink(missing_ok=True)
        raise UpdateError("The download does not match the release checksum.")
    part.replace(dest)


def prepare(target: InstallTarget, update: Update, archive: Path) -> Path:
    """Turn the verified download into what apply_update installs from."""
    if target.kind == "windows":
        return archive
    if target.kind == "linux":
        archive.chmod(0o755)
        return archive
    app = target.version_dir(update) / _APP_BUNDLE
    # Extracted and checked under another name, so a failed or interrupted
    # copy never sits where ready_payload and apply_update look
    partial = app.with_name(_APP_BUNDLE + ".partial")
    for stale in (app, partial):
        if stale.exists():
            shutil.rmtree(stale)
    try:
        with tempfile.TemporaryDirectory() as mount:
            subprocess.run(
                ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mount, str(archive)],
                check=True, capture_output=True,
            )
            try:
                # ditto keeps the bundle's symlinks and its ad-hoc signature
                subprocess.run(["ditto", str(Path(mount) / _APP_BUNDLE), str(partial)], check=True, capture_output=True)
            finally:
                subprocess.run(["hdiutil", "detach", mount], check=False, capture_output=True)
        _check_bundle(partial, update.version)
        partial.rename(app)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return app


def _check_bundle(app: Path, version: str) -> None:
    try:
        with (app / "Contents" / "Info.plist").open("rb") as f:
            found = plistlib.load(f).get("CFBundleShortVersionString")
    except (OSError, plistlib.InvalidFileException) as exc:
        raise UpdateError(f"The downloaded app is incomplete: {exc}") from exc
    if found != version or not (app / "Contents" / "MacOS" / "PAM-Analyzer").is_file():
        raise UpdateError(f"The downloaded app is version {found}, expected {version}.")


def verified_archive(target: InstallTarget, update: Update) -> Path | None:
    """The downloaded file from an earlier session, if it still matches the digest."""
    archive = target.archive_path(update)
    if update.sha256 is None or not archive.is_file() or _sha256_of(archive) != update.sha256:
        return None
    return archive


def ready_payload(target: InstallTarget, update: Update) -> Path | None:
    """The prepared payload from an earlier download, if it is still intact."""
    archive = verified_archive(target, update)
    if archive is None:
        return None
    if target.kind != "macos":
        return archive
    app = target.version_dir(update) / _APP_BUNDLE
    try:
        _check_bundle(app, update.version)
    except UpdateError:
        return None
    return app


def fetch_payload(
    target: InstallTarget,
    update: Update,
    user_agent: str,
    progress: Callable[[int, int], None],
    cancelled: Callable[[], bool],
    *,
    reuse_only: bool = False,
) -> Path | None:
    """The payload apply_update installs, from an earlier session or downloaded now.

    Staged versions other than update's are deleted first. With reuse_only
    nothing is downloaded and the result is None when nothing usable is staged.
    """
    _prune_staged(target, keep=lambda name: name == update.version)
    payload = ready_payload(target, update)
    if payload is not None:
        return payload
    # The download survived but its extracted app did not
    archive = verified_archive(target, update)
    if archive is None:
        if reuse_only:
            return None
        archive = target.archive_path(update)
        download(update, archive, user_agent, progress, cancelled)
    return prepare(target, update, archive)


def relaunch_env(environ: Mapping[str, str], bundle_dir: Path) -> dict[str, str]:
    """The environment for the new version, without a model path inside bundle_dir.

    app/__main__.py points BIRDNET_APP_DATA into this version's bundle, on
    Linux the AppImage mount that vanishes when this process exits. A path the
    user set elsewhere stays.
    """
    env = dict(environ)
    model_path = env.get("BIRDNET_APP_DATA")
    if model_path and Path(model_path).is_relative_to(bundle_dir):
        del env["BIRDNET_APP_DATA"]
    return env


def _launch_detached(command: list[str]) -> None:
    """Start command so that it outlives this process."""
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": relaunch_env(os.environ, Path(sys.executable).parent),
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(command, **kwargs)  # noqa: S603  the command is built from our own paths


def apply_update(
    target: InstallTarget, payload: Path, launch: Callable[[list[str]], None] | None = None
) -> None:
    """Install payload and start the new version. Called while the app exits.

    Windows hands over to the installer, which waits for this process through
    the running mutex. macOS and Linux rename the running app aside, which
    both allow, and move the new one into place. A failed rename puts the old
    app back and starts it again. Never raises, the caller is shutting down.
    """
    launch = launch or _launch_detached
    if target.kind == "windows":
        _launch_logged(target, launch, [str(payload), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/RELAUNCH"])
        return
    app, old = target.app_path, target.old_path
    start = ["open", "-n", str(app)] if target.kind == "macos" else [str(app)]
    _remove(old)
    try:
        app.rename(old)
        payload.rename(app)
    except OSError as exc:
        _log.error("could not install the update from %s: %s", payload, exc)
        _note_failure(target, exc)
        if old.exists() and not app.exists():
            try:
                old.rename(app)
            except OSError as restore_exc:
                _log.error("could not restore %s: %s", app, restore_exc)
    _launch_logged(target, launch, start)


def _note_failure(target: InstallTarget, exc: OSError) -> None:
    try:
        target.staging_dir.mkdir(parents=True, exist_ok=True)
        (target.staging_dir / _APPLY_FAILED).write_text(str(exc), encoding="utf-8")
    except OSError:
        pass  # the caller's log line still records it


def _launch_logged(target: InstallTarget, launch: Callable[[list[str]], None], command: list[str]) -> None:
    # The app is exiting, so a failed start, say setup.exe removed from %TEMP%,
    # is logged and noted for the next session instead of raised
    try:
        launch(command)
    except OSError as exc:
        _log.error("could not start %s: %s", command[0], exc)
        _note_failure(target, exc)


def take_apply_failure(target: InstallTarget) -> str | None:
    """The error from the last failed swap, deleted once read, or None.

    Read once, so one session explains the failure and the next tries again,
    in case the user has since granted the permission that was missing.
    """
    note = target.staging_dir / _APPLY_FAILED
    try:
        text = note.read_text(encoding="utf-8")
        note.unlink()
    except OSError:
        return None
    return text or "unknown error"
