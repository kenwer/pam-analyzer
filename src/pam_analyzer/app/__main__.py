"""Application entry point. Builds the object graph and launches the UI."""

import gc
import logging
import os
import sys
import tempfile
from pathlib import Path


def _configure_frozen_model_paths() -> None:
    """Point the birdnet lib at the bundled model cache.

    The build stages the birdnet-models/ tree next to the compiled binary.
    BIRDNET_APP_DATA has to be set before any `import birdnet` call triggers a
    model load, or the frozen app will try to write to the user's home
    directory and re-download.

    setdefault() rather than [] = so a user can still override the variable
    from the shell for development or one-off builds.

    No-op when not running frozen, so dev runs still use the per-user
    cache and stay independent of the build artifact.
    """
    # Nuitka sets neither sys.frozen nor sys._MEIPASS. It defines __compiled__
    # in the globals of every compiled module, and sys.executable points at a
    # python stub beside the binary whose parent holds the payload, inside a
    # macOS .app too.
    if "__compiled__" not in globals():
        return
    bundled = Path(sys.executable).parent / "birdnet-models"
    os.environ.setdefault("BIRDNET_APP_DATA", str(bundled / "birdnet-app-data"))


_configure_frozen_model_paths()


from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ..domain import logging_setup, paths  # noqa: E402
from ..infrastructure import (  # noqa: E402
    AudioImporter,
    Birdnet24Runner,
    BirdnetRunner,
    PerchRunner,
    PsutilSdCardScanner,
    SoundfileAudioExtractor,
)
from ..ui import resources_rc  # noqa: F401, E402  registers :/icons/* resources
from ..ui.app_state import AppState  # noqa: E402
from ..ui.main_window import MainWindow  # noqa: E402
from ..ui.settings import AppSettings  # noqa: E402
from ..workers import ImportOrchestrator  # noqa: E402


def build_main_window(settings: AppSettings) -> MainWindow:
    audio_extractor = SoundfileAudioExtractor()
    analysis_runners = {r.model_key: r for r in (Birdnet24Runner(), BirdnetRunner(), PerchRunner())} # Insertion order is the combo order
    sdcard_scanner = PsutilSdCardScanner()
    audio_importer = AudioImporter()
    import_orchestrator = ImportOrchestrator(audio_importer, sdcard_scanner)

    app_state = AppState()
    return MainWindow(
        app_state,
        analysis_runners,
        import_orchestrator,
        settings,
        audio_extractor,
    )


def main() -> int:
    settings = AppSettings()
    levels = logging.getLevelNamesMapping()
    env_level = os.environ.get("PAM_LOG_LEVEL", "").upper()
    # An explicit env var is the developer override. It wins over the persisted choice and locks the menu.
    locked = env_level in levels
    level = levels[env_level] if locked else levels[settings.log_level]
    logging_setup.configure(paths.log_dir() / "pam-analyzer.log", level, locked=locked)
    logging.getLogger(__name__).debug(
        "BIRDNET_APP_DATA=%s", os.environ.get("BIRDNET_APP_DATA", "<unset, per-user cache>")
    )

    app = QApplication(sys.argv)
    app.setApplicationName("PAM Analyzer")
    app.setOrganizationName("PAM Analyzer")
    app.setWindowIcon(QIcon(":/icons/icon.svg"))
    window = build_main_window(settings)

    # Signal the splash screen removal to nuitka
    if "NUITKA_ONEFILE_PARENT" in os.environ:
        splash_filename = os.path.join(
            tempfile.gettempdir(),
            f"onefile_{int(os.environ['NUITKA_ONEFILE_PARENT'])}_splash_feedback.tmp",
        )
        if os.path.exists(splash_filename):
            os.unlink(splash_filename)

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
