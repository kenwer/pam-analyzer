"""Application entry point. Builds the object graph and launches the UI."""

import gc
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from platformdirs import user_log_dir
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from ..infrastructure import (
    AudioImporter,
    BirdnetAnalyzerRunner,
    CsvDetectionRepository,
    PsutilSdCardScanner,
    SoundfileAudioExtractor,
    TomlCampaignRepository,
    TomlProjectRepository,
)
from ..workers import ImportOrchestrator
from ..ui import resources_rc  # noqa: F401  registers :/icons/* resources
from ..ui.app_state import AppState
from ..ui.main_window import MainWindow
from .settings import AppSettings


def build_main_window() -> MainWindow:
    project_repo = TomlProjectRepository()
    campaign_repo = TomlCampaignRepository()
    detection_repo = CsvDetectionRepository()
    audio_extractor = SoundfileAudioExtractor()
    analysis_runner = BirdnetAnalyzerRunner()
    sdcard_scanner = PsutilSdCardScanner()
    audio_importer = AudioImporter()
    import_orchestrator = ImportOrchestrator(audio_importer, sdcard_scanner)

    app_state = AppState(project_repo, campaign_repo)
    settings = AppSettings()
    return MainWindow(
        app_state,
        campaign_repo,
        detection_repo,
        analysis_runner,
        import_orchestrator,
        settings,
        audio_extractor,
    )


def _setup_logging(level: int) -> None:
    log_dir = Path(user_log_dir("PAM Analyzer", appauthor=False))
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
