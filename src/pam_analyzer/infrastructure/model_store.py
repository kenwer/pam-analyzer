"""Download the files an engine needs and tell which ones are missing.

Each engine declares a ModelPackage: its pinned files plus an optional step
that derives further files from them. The store knows nothing about models.

An artifact is current when a marker file next to it names the sha256 the
app pins. The marker is written only after the file is verified and in place,
so an interrupted install never counts as present. Comparing markers instead
of hashing keeps the check cheap enough to run before every analysis, and it
still notices a changed pin for files whose path never changes.

Engines download in the background and some share files, so installs run
one at a time. A second install waits for the first and then finds what they
share already current. Parallel downloads would only split the bandwidth.
"""

import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from ..domain import ModelInfo
from .http_download import DownloadCancelled, DownloadError, download_file, sha256_of


@dataclass(frozen=True)
class Artifact:
    """One pinned download.

    stale_glob names older siblings of dest to delete once the pinned file is
    in place, for files whose name carries their checksum.
    """

    id: str
    url: str
    size: int
    sha256: str
    dest: Path
    stale_glob: str | None = None


@dataclass(frozen=True)
class ModelPackage:
    """Everything one engine needs on disk.

    artifacts is called on use, not at import, because the paths depend on
    BIRDNET_APP_DATA. prepare builds files derived from the artifacts. It runs
    after every install, offline, so it must be cheap when nothing changed.
    Engines that share a file list the same Artifact, and the store fetches
    it once.
    """

    info: ModelInfo
    artifacts: Callable[[], tuple[Artifact, ...]]
    prepare: Callable[[], None] | None = None


_install_lock = threading.Lock()


def marker_path(artifact: Artifact) -> Path:
    return artifact.dest.with_name(artifact.dest.name + ".sha256")


def _is_current(artifact: Artifact) -> bool:
    try:
        if marker_path(artifact).read_text(encoding="ascii").strip() != artifact.sha256:
            return False
    except (OSError, UnicodeDecodeError):
        return False
    return artifact.dest.is_file()


def missing(artifacts: Iterable[Artifact]) -> list[Artifact]:
    """The artifacts without a current marker, each shared one listed once."""
    return [a for a in dict.fromkeys(artifacts) if not _is_current(a)]


def missing_bytes(artifacts: Iterable[Artifact]) -> int:
    """The download size of missing().

    It over-counts a file placed before markers existed, which install()
    adopts after one hash instead of downloading it.
    """
    return sum(a.size for a in missing(artifacts))


def download(
    artifacts: Iterable[Artifact],
    progress: Callable[[int, int], None],
    cancelled: Callable[[], bool],
) -> None:
    """Download every missing artifact, reporting bytes across the whole batch.

    Raises DownloadError, also for a failure to write the marker, and
    DownloadCancelled. Artifacts finished before a failure stay installed.
    """
    todo = missing(artifacts)
    if not todo:
        return
    logging.info("Downloading %s", ", ".join(a.id for a in todo))
    total = sum(a.size for a in todo)
    done = 0
    progress(done, total)
    for artifact in todo:
        if not _adopt(artifact):
            base = done
            download_file(
                artifact.url,
                artifact.size,
                artifact.sha256,
                artifact.dest,
                lambda d, _t, base=base: progress(base + d, total),
                cancelled,
            )
            try:
                _finalize(artifact)
            except OSError as exc:
                raise DownloadError(f"{artifact.id}: {exc}") from exc
        done += artifact.size
        progress(done, total)


def install(
    package: ModelPackage,
    progress: Callable[[int, int], None],
    cancelled: Callable[[], bool],
) -> None:
    """Download what the package is missing, then run its prepare step.

    Waits while another install runs, and can be cancelled while waiting.
    Raises DownloadError, also when prepare fails, and DownloadCancelled.
    """
    while not _install_lock.acquire(timeout=0.1):
        if cancelled():
            raise DownloadCancelled
    try:
        download(package.artifacts(), progress, cancelled)
        if package.prepare is not None:
            try:
                package.prepare()
            except Exception as exc:  # noqa: BLE001  prepare runs third-party code with its own error types
                raise DownloadError(f"Preparing the downloaded files failed: {exc}") from exc
    finally:
        _install_lock.release()


def _adopt(artifact: Artifact) -> bool:
    """Mark a file that is already in place as current if its hash matches the pin.

    Files placed before markers existed (older builds, dev scripts, a CI cache)
    then cost one hash instead of a download.
    """
    if not artifact.dest.is_file():
        return False
    if artifact.dest.stat().st_size != artifact.size or sha256_of(artifact.dest) != artifact.sha256:
        return False
    try:
        _finalize(artifact)
    except OSError as exc:
        raise DownloadError(f"{artifact.id}: {exc}") from exc
    return True


def _finalize(artifact: Artifact) -> None:
    """Drop stale siblings and write the marker of a verified download."""
    if artifact.stale_glob is not None:
        for sibling in artifact.dest.parent.glob(artifact.stale_glob):
            if sibling != artifact.dest:
                sibling.unlink()
    marker_path(artifact).write_text(artifact.sha256 + "\n", encoding="ascii")
