"""Resumable HTTPS download of one file, verified against its sha256.

Shared by the app update and the model downloads. Certificates are checked
against the OS trust store, so a corporate proxy's root CA is honoured.
"""

import hashlib
import http.client
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

import truststore

from .. import __version__
from ..domain import CancelledError

USER_AGENT = f"PAM-Analyzer/{__version__}"
TLS_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

_CHUNK = 1 << 20


class DownloadError(Exception):
    """A network failure, an HTTP error status, or a digest mismatch."""


class DownloadCancelled(CancelledError):
    """The user cancelled. The partial file stays, so the next download resumes."""


def sha256_of(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def download_file(
    url: str,
    size: int,
    sha256: str,
    dest: Path,
    progress: Callable[[int, int], None],
    cancelled: Callable[[], bool],
) -> None:
    """Download url to dest, resuming a partial file, and verify its sha256.

    size may be 0 when unknown, then the Content-Length decides the total.
    Raises DownloadError on a network failure or a mismatch, and
    DownloadCancelled when cancelled() turns true.
    """
    host = urllib.parse.urlsplit(url).hostname or url
    part = dest.with_name(dest.name + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    have = part.stat().st_size if part.exists() else 0
    if size and have >= size:
        # Complete already, say killed between the last write and the rename.
        # A Range request past the end would only get a 416.
        _finish(part, dest, sha256_of(part), sha256, host)
        return
    headers = {"User-Agent": USER_AGENT}
    if have:
        headers["Range"] = f"bytes={have}-"
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=30, context=TLS_CONTEXT) as response:
            if response.status == 206:
                with part.open("rb") as existing:
                    digest = hashlib.file_digest(existing, "sha256")
                mode = "ab"
            else:
                digest = hashlib.sha256()
                have, mode = 0, "wb"
            expected_end = have + int(response.headers.get("Content-Length", 0))
            total = size or expected_end
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
                raise DownloadError(f"The download from {host} was interrupted. It resumes on the next try.")
    except urllib.error.HTTPError as exc:
        if exc.code == 416:  # the .part does not fit the file on the server
            part.unlink(missing_ok=True)
        raise DownloadError(f"{host} answered {exc.code} {exc.reason}.") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise DownloadError(f"Could not download from {host}: {exc}") from exc
    _finish(part, dest, digest.hexdigest(), sha256, host)


def _finish(part: Path, dest: Path, actual: str, expected: str, host: str) -> None:
    if actual != expected:
        part.unlink(missing_ok=True)
        raise DownloadError(f"The file from {host} does not match its checksum.")
    part.replace(dest)
