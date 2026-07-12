"""Application entry point. Builds the object graph and launches the UI."""

import gc
import logging
import logging.handlers
import os
import sys
from pathlib import Path


def _configure_frozen_model_paths() -> None:
    """Point the birdnet lib and kagglehub at the bundled model cache.

    PyInstaller stages our bundled birdnet-models/ tree under sys._MEIPASS at
    runtime. Both BIRDNET_APP_DATA (read by the birdnet library) and
    KAGGLEHUB_CACHE (read by kagglehub, which the lib uses for Perch v2)
    have to be set before any `import birdnet` call triggers a model load,
    or the frozen app will try to write to the user's home directory and
    re-download.

    setdefault() rather than [] = so a user can still override either
    variable from the shell for development or one-off builds.

    No-op when not running frozen, so dev runs still use the per-user
    cache and stay independent of the build artifact.
    """
    if not getattr(sys, "frozen", False):
        return
    base = Path(getattr(sys, "_MEIPASS", "."))
    bundled = base / "birdnet-models"
    os.environ.setdefault("BIRDNET_APP_DATA", str(bundled / "birdnet-app-data"))
    os.environ.setdefault("KAGGLEHUB_CACHE", str(bundled / "kagglehub"))
    _pin_kagglehub_to_bundled_version()


def _pin_kagglehub_to_bundled_version() -> None:
    """Resolve Kaggle model versions from the bundled cache instead of the network.

    The birdnet library requests Perch v2 with an unversioned Kaggle handle, so
    kagglehub.model_download() calls the Kaggle API once per run to learn the
    current version number before it consults the cache (see
    kagglehub.http_resolver._get_current_version, which GETs models/.../get).
    That metadata call is cheap, but it makes an otherwise self-contained frozen
    build require network access, and a newly published upstream version would
    miss the cache and trigger a real re-download into the read-only bundle.

    The on-disk cache is version-keyed
    (<KAGGLEHUB_CACHE>/models/<owner>/<model>/<framework>/<variation>/<version>),
    so the bundled directory name is the version. Replace _get_current_version
    to return that local version for any model handle already present in the
    cache. Non-model handles, or models not found locally, fall back to the
    original network implementation so a first-time dev cache still fills.

    Only invoked from _configure_frozen_model_paths (frozen builds), so dev runs
    keep kagglehub's stock behavior.
    """
    try:
        from kagglehub import http_resolver
        from kagglehub.config import get_cache_folder
        from kagglehub.handle import ModelHandle
    except Exception:  # kagglehub internals moved or it is absent; leave as-is.
        return

    original_get_current_version = http_resolver._get_current_version

    def _version_from_cache(api_client: object, h: object) -> int:
        if isinstance(h, ModelHandle):
            variation_dir = (
                Path(get_cache_folder())
                / "models"
                / h.owner
                / h.model
                / h.framework
                / h.variation
            )
            versions = [
                int(child.name)
                for child in variation_dir.glob("*")
                if child.is_dir() and child.name.isdigit()
            ]
            if versions:
                return max(versions)
        return original_get_current_version(api_client, h)

    http_resolver._get_current_version = _version_from_cache


_configure_frozen_model_paths()


from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ..infrastructure import (  # noqa: E402
    AudioImporter,
    BirdnetRunner,
    CsvDetectionRepository,
    PerchRunner,
    PsutilSdCardScanner,
    SoundfileAudioExtractor,
    TomlCampaignRepository,
    TomlProjectRepository,
    paths,
)
from ..ui import resources_rc  # noqa: F401, E402  registers :/icons/* resources
from ..ui.app_state import AppState  # noqa: E402
from ..ui.main_window import MainWindow  # noqa: E402
from ..ui.settings import AppSettings  # noqa: E402
from ..workers import ImportOrchestrator  # noqa: E402


def build_main_window() -> MainWindow:
    project_repo = TomlProjectRepository()
    campaign_repo = TomlCampaignRepository()
    detection_repo = CsvDetectionRepository()
    audio_extractor = SoundfileAudioExtractor()
    # Ordered: first key is the default model in the panel's dropdown.
    # Keying by model_key keeps a single source of truth for model identity.
    analysis_runners = {r.model_key: r for r in (BirdnetRunner(), PerchRunner())}
    sdcard_scanner = PsutilSdCardScanner()
    audio_importer = AudioImporter()
    import_orchestrator = ImportOrchestrator(audio_importer, sdcard_scanner)

    app_state = AppState(project_repo, campaign_repo)
    settings = AppSettings()
    return MainWindow(
        app_state,
        campaign_repo,
        detection_repo,
        analysis_runners,
        import_orchestrator,
        settings,
        audio_extractor,
    )


def _setup_logging(level: int) -> None:
    log_dir = paths.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "pam-analyzer.log"

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=1, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    logging.getLogger(__name__).debug("Logging to %s", log_file)


def main() -> int:
    level_name = os.environ.get("PAM_LOG_LEVEL", "WARNING").upper()
    _setup_logging(getattr(logging, level_name, logging.WARNING))

    app = QApplication(sys.argv)
    app.setApplicationName("PAM Analyzer")
    app.setOrganizationName("PAM Analyzer")
    app.setWindowIcon(QIcon(":/icons/icon.svg"))
    window = build_main_window()
    window.show()
    exit_code = app.exec()
    # Destroy Qt objects while Python is fully operational. Without this,
    # PySide6's atexit handler (SbkQtCoreModule___moduleShutdown) may call
    # QApplication::~QApplication() after Qt internals are already freed,
    # causing a SIGSEGV. gc.collect() resolves any circular references that
    # would otherwise keep the objects alive past interpreter shutdown.
    del window
    del app
    gc.collect()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
