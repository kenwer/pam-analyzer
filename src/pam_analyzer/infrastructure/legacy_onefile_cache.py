"""Removes the unpack caches the onefile builds (0.8.x and earlier) left behind.

Those builds unpacked into {CACHE_DIR}/{COMPANY}/{PRODUCT}/{VERSION}, about
1.6 GB per version, and nothing ever deleted them. Installer builds no longer
unpack, so the whole {COMPANY}/{PRODUCT} folder is dead weight.
"""

import logging
import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

_log = logging.getLogger(__name__)

# The --company-name and --product-name the onefile builds were compiled with
_COMPANY = "de.ken"
_PRODUCT = "pam-analyzer"


def legacy_cache_dir(platform: str = sys.platform, environ: Mapping[str, str] = os.environ) -> Path | None:
    """Where the onefile builds unpacked to, or None on macOS, which never shipped onefile.

    Mirrors Nuitka's {CACHE_DIR}: CSIDL_LOCAL_APPDATA on Windows, and
    $XDG_CACHE_HOME or ~/.cache elsewhere.
    """
    if platform == "darwin":
        return None
    if platform == "win32":
        base = environ.get("LOCALAPPDATA")
        if not base:
            return None
        return Path(base) / _COMPANY / _PRODUCT
    base = environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / _COMPANY / _PRODUCT


def remove_legacy_cache(cache_dir: Path | None) -> None:
    """Delete cache_dir, then its company folder if that is left empty.

    Errors are logged, not raised. A file still in use, say by an old onefile
    instance running alongside, keeps its folder until a later launch retries.
    """
    if cache_dir is None or not cache_dir.is_dir():
        return
    _log.info("removing legacy onefile cache %s", cache_dir)
    shutil.rmtree(cache_dir, onexc=lambda _fn, path, exc: _log.warning("could not remove %s: %s", path, exc))
    company_dir = cache_dir.parent
    try:
        company_dir.rmdir()  # only succeeds when empty
    except OSError:
        pass
