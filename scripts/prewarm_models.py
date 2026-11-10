"""Install every engine's model package into BIRDNET_APP_DATA through the app's model store.

CI fills its model cache with this, so the cache carries the same markers and
generated label files a user's first run writes, and the tests never download.
Without BIRDNET_APP_DATA it fills the per-user folder the app and the local
slow tests read. Run from the project venv:

    uv run poe prewarm
"""

import sys
import time
from collections.abc import Iterable

from pam_analyzer.infrastructure import model_store
from pam_analyzer.infrastructure.birdnet_2_4_runner import Birdnet24Runner
from pam_analyzer.infrastructure.birdnet_runner import BirdnetRunner
from pam_analyzer.infrastructure.http_download import DownloadError
from pam_analyzer.infrastructure.model_store import ModelPackage
from pam_analyzer.infrastructure.perch_runner import PerchRunner

_ATTEMPTS = 3


def _progress(done: int, total: int) -> None:
    print(f"\r  {done >> 20} / {total >> 20} MB", end="", file=sys.stderr, flush=True)


def prewarm(packages: Iterable[ModelPackage], retry_delay_s: float = 10) -> None:
    """Install each package, retrying a failed install.

    A retry resumes partial files, so a dropped connection costs little.
    """
    for package in packages:
        print(f"{package.info.name}: {model_store.missing_bytes(package.artifacts()) >> 20} MB to download", file=sys.stderr)
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                model_store.install(package, _progress, lambda: False)
                break
            except DownloadError as exc:
                if attempt == _ATTEMPTS:
                    raise
                print(f"\n  {exc} Retrying ({attempt}/{_ATTEMPTS})...", file=sys.stderr)
                time.sleep(retry_delay_s)
        print(file=sys.stderr)


def main() -> int:
    prewarm(runner.package for runner in (Birdnet24Runner, BirdnetRunner, PerchRunner))
    print("All models cached.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
