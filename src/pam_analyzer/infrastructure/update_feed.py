"""The GitHub release feed: is there a newer version, and which file is it."""

import http.client
import json
import platform
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

from packaging.version import InvalidVersion, Version

DEFAULT_FEED_URL = "https://api.github.com/repos/kenwer/pam-analyzer/releases/latest"

_SHA256 = re.compile(r"sha256:([0-9a-f]{64})")


class UpdateError(Exception):
    """The feed or a download failed: network, HTTP status, bad data or a digest mismatch."""


@dataclass(frozen=True)
class Update:
    version: str
    notes: str
    page_url: str
    asset_name: str
    asset_url: str
    size: int
    # None when the release carries no usable digest, which rules out an install
    sha256: str | None


def release_asset_name(system: str = sys.platform, machine: str = platform.machine()) -> str | None:
    """The installer asset for this platform, named as scripts/build.py names it."""
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(machine.lower())
    if arch is None:
        return None
    if system == "darwin":
        return f"PAM-Analyzer-macos-{arch}.dmg"
    if system == "win32":
        return f"PAM-Analyzer-windows-{arch}-setup.exe"
    if system.startswith("linux"):
        return f"PAM-Analyzer-linux-{arch}.AppImage"
    return None


def fetch_latest_release(url: str, current_version: str, timeout: float = 10.0) -> dict:
    """The release JSON at url. Every failure becomes UpdateError."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"PAM-Analyzer/{current_version}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except (OSError, http.client.HTTPException, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UpdateError(f"Could not read the release feed: {exc}") from exc


def find_update(release: dict, current_version: str, asset_name: str | None) -> Update | None:
    """The update release offers over current_version, or None.

    No update when either version does not parse, the release is not newer,
    or it lacks the platform's asset. A release of an unexpected shape raises
    UpdateError.
    """
    try:
        return _find_update(release, current_version, asset_name)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise UpdateError(f"The release feed has an unexpected format: {exc!r}") from exc


def _find_update(release: dict, current_version: str, asset_name: str | None) -> Update | None:
    try:
        offered = Version(str(release.get("tag_name", "")).removeprefix("v"))
        if offered <= Version(current_version):
            return None
    except InvalidVersion:
        return None
    asset = next((a for a in release.get("assets", []) if a.get("name") == asset_name), None)
    if asset_name is None or asset is None:
        return None
    match = _SHA256.fullmatch(str(asset.get("digest", "")))
    return Update(
        version=str(offered),
        notes=release.get("body") or "",
        page_url=release.get("html_url", ""),
        asset_name=asset_name,
        asset_url=asset["browser_download_url"],
        size=int(asset.get("size", 0)),
        sha256=match.group(1) if match else None,
    )
