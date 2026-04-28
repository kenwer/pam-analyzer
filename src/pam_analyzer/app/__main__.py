"""Application entry point. Builds the object graph and launches the UI."""

import gc
import logging
import os
import sys

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

    app_state = AppState(project_repo, campaign_repo)
    settings = AppSettings()
    return MainWindow(
        app_state,
        campaign_repo,
        detection_repo,
        analysis_runner,
        audio_importer,
        sdcard_scanner,
        settings,
        audio_extractor,
    )


def main() -> int:
    level_name = os.environ.get("PAM_LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.WARNING),
        format="%(levelname)s %(name)s: %(message)s",
    )

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
