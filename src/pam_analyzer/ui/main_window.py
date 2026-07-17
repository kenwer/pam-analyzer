import sys
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from ..domain import AnalysisRunner
from ..domain.audio_import import ImportSource
from ..infrastructure import (
    CsvDetectionRepository,
    SoundfileAudioExtractor,
    TomlCampaignRepository,
    find_legacy_pamproj,
    load_legacy,
    migrate,
    paths,
)
from ..workers import ImportOrchestrator
from .app_state import AppState
from .dialogs.about_dialog import show_about_dialog
from .panels.birdnet_panel import BirdNetPanel
from .panels.campaigns_panel import CampaignsPanel
from .panels.examine_panel import ExaminePanel
from .panels.project_panel import ProjectPanel
from .panels.welcome_panel import WelcomePanel
from .settings import AppSettings
from .ui_main_window import Ui_MainWindow


class MainWindow(QMainWindow):
    def __init__(
        self,
        app_state: AppState,
        campaign_repo: TomlCampaignRepository,
        detections_repo: CsvDetectionRepository,
        analysis_runners: dict[str, AnalysisRunner],
        import_orchestrator: ImportOrchestrator,
        settings: AppSettings,
        audio_extractor: SoundfileAudioExtractor,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)

        self._app_state = app_state
        self._settings = settings
        self._analysis_running = False
        self._import_running = False

        # Welcome page: mount WelcomePanel into the stacked widget's first page
        self._welcome_panel = WelcomePanel(self.ui.welcome_page)
        welcome_layout = QVBoxLayout(self.ui.welcome_page)
        welcome_layout.setContentsMargins(0, 0, 0, 0)
        welcome_layout.addWidget(self._welcome_panel)

        self._campaigns_panel = CampaignsPanel(
            app_state,
            campaign_repo,
            import_orchestrator,
            settings,
            self.ui.campaigns_tab,
        )
        self._mount_tab(self.ui.campaigns_tab, self._campaigns_panel, "Campaigns")

        self._project_panel = ProjectPanel(app_state, self.ui.project_tab)
        self._mount_tab(self.ui.project_tab, self._project_panel, "Project")

        # The Import Audio tab is gone in step 3: imports now live inside the
        # campaign view. Remove the placeholder so users don't see an empty tab.
        import_idx = self.ui.tab_widget.indexOf(self.ui.import_tab)
        if import_idx != -1:
            self.ui.tab_widget.removeTab(import_idx)

        self._birdnet_panel = BirdNetPanel(app_state, analysis_runners, campaign_repo, self.ui.birdnet_tab)
        self._mount_tab(self.ui.birdnet_tab, self._birdnet_panel, "BirdNET")

        self._examine_panel = ExaminePanel(
            app_state,
            detections_repo,
            settings,
            audio_extractor,
            self.ui.examine_tab,
        )
        self._mount_tab(self.ui.examine_tab, self._examine_panel, "Examine")
        self.ui.tab_widget.setCurrentWidget(self._project_panel)

        self._wire_actions()
        self._wire_state()
        self._wire_welcome()
        self._restore_geometry()
        self._refresh_action_state(None)
        self._rebuild_recent_menu()
        self._show_welcome()
        self._splash_closed = False  # track whether we've already closed the splash

    def _mount_tab(self, placeholder: QWidget, panel: QWidget, label: str) -> None:
        idx = self.ui.tab_widget.indexOf(placeholder)
        self.ui.tab_widget.removeTab(idx)
        self.ui.tab_widget.insertTab(idx, panel, label)

    # wiring

    def _wire_actions(self) -> None:
        self.ui.action_new.triggered.connect(self._on_new)
        self.ui.action_open.triggered.connect(self._on_open)
        self.ui.action_close.triggered.connect(self._on_close_project)
        self.ui.action_clear_recent.triggered.connect(self._on_clear_recent)
        self.ui.action_quit.triggered.connect(self.close)
        self.ui.action_open_log_folder.triggered.connect(self._on_open_log_folder)
        self.ui.action_about.triggered.connect(self._on_about)

    def _wire_state(self) -> None:
        self._app_state.statusMessage.connect(lambda msg: self.ui.status_bar.showMessage(msg, 5000))
        self._app_state.errorOccurred.connect(lambda msg: QMessageBox.warning(self, "PAM Analyzer", msg))
        self._app_state.projectChanged.connect(self._on_project_changed)
        self._app_state.analysisStarted.connect(self._on_analysis_started)
        self._app_state.analysisFinished.connect(self._on_analysis_finished)
        self._app_state.importStarted.connect(self._on_import_started)
        self._app_state.importFinished.connect(self._on_import_finished)

    def _on_analysis_started(self) -> None:
        self._analysis_running = True
        self._update_tab_lock()
        self.ui.status_bar.showMessage("BirdNET running…", 0)

    def _on_analysis_finished(self, _result: object) -> None:
        self._analysis_running = False
        self._update_tab_lock()
        self.ui.status_bar.clearMessage()

    def _on_import_started(self, campaign_name: str, source: ImportSource) -> None:
        self._import_running = True
        self._update_tab_lock()
        message = (
            f"Importing audio from folder (campaign: {campaign_name})…"
            if source is ImportSource.FOLDER
            else f"Watching for SD cards (campaign: {campaign_name})…"
        )
        self.ui.status_bar.showMessage(message, 0)

    def _on_import_finished(self) -> None:
        self._import_running = False
        self._update_tab_lock()
        self.ui.status_bar.clearMessage()

    def _update_tab_lock(self) -> None:
        locked = self._analysis_running or self._import_running
        tab_bar = self.ui.tab_widget.tabBar()
        current = self.ui.tab_widget.currentIndex()
        for i in range(self.ui.tab_widget.count()):
            enabled = not locked or i == current
            tab_bar.setTabEnabled(i, enabled)
            # macOS Aqua style ignores the disabled state visually; override the text
            # color at the Qt level so the lock is perceptible. Other platforms render
            # disabled tabs correctly on their own. QColor() resets to palette default.
            if sys.platform == "darwin":
                tab_bar.setTabTextColor(i, QColor() if enabled else QColor(128, 128, 128, 160))

    def _wire_welcome(self) -> None:
        self._welcome_panel.newRequested.connect(self._on_new)
        self._welcome_panel.openRequested.connect(self._on_open)
        self._welcome_panel.recentRequested.connect(self._open_recent)

    # File menu handlers

    def _on_new(self) -> None:
        folder_str = QFileDialog.getExistingDirectory(
            self,
            "Choose or create the project folder",
            self._settings.last_directory,
        )
        if folder_str:
            self._open_folder(Path(folder_str), confirm_create=False)

    def _on_open(self) -> None:
        folder_str = QFileDialog.getExistingDirectory(
            self,
            "Open project folder",
            self._settings.last_directory,
        )
        if folder_str:
            self._open_folder(Path(folder_str), confirm_create=True)

    def _open_folder(self, folder: Path, *, confirm_create: bool) -> None:
        """Open folder as a project, offering legacy migration or initialization.

        New and Open share this path so a legacy .pamproj folder is detected
        either way; they differ only in whether initializing a fresh folder
        needs confirmation.
        """
        if paths.project_toml(folder).exists():
            self._load_and_remember(folder)
            return
        legacy = find_legacy_pamproj(folder)
        if legacy is not None:
            self._migrate_legacy(legacy)
            return
        if confirm_create:
            choice = QMessageBox.question(
                self,
                "Not a project",
                f"'{folder.name}' is not a PAM Analyzer project.\n"
                "Initialize it as a new project?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return
        if not self._confirm_cancel_running():
            return
        self._app_state.create_project(folder)
        if self._app_state.project is not None:
            self._remember(folder)

    def _load_and_remember(self, folder: Path) -> None:
        if not self._confirm_cancel_running():
            return
        self._app_state.load_project(folder)
        if self._app_state.project is not None:
            self._remember(folder)

    def _migrate_legacy(self, pamproj_path: Path) -> None:
        """Offer and run the one-time conversion of a legacy .pamproj project."""
        try:
            legacy = load_legacy(pamproj_path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Cannot migrate project", str(exc))
            return
        choice = QMessageBox.question(
            self,
            "Migrate project?",
            f"'{pamproj_path.name}' uses the old project format.\n\n"
            f"Migrating will:\n"
            f"- write {paths.PROJECT_FILENAME} into {legacy.audio_root}\n"
            f"- move detection CSVs into their campaign folders\n"
            f"- keep the old file as {pamproj_path.name}.bak\n\n"
            f"Migrate now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return
        if not self._confirm_cancel_running():
            return
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                report = migrate(legacy)
            finally:
                # Restore before any dialog so the message box gets a normal cursor.
                QApplication.restoreOverrideCursor()
        except Exception as exc:  # noqa: BLE001  surface any filesystem failure to the user
            QMessageBox.warning(self, "Migration failed", str(exc))
            return
        if report.warnings:
            QMessageBox.information(
                self, "Migration finished with warnings", "\n".join(report.warnings)
            )
        self._settings.remove_recent_project(str(pamproj_path))
        self._load_and_remember(report.project_folder)

    def _on_close_project(self) -> None:
        if not self._confirm_cancel_running():
            return
        self._app_state.close_project()

    def _on_clear_recent(self) -> None:
        self._settings.clear_recent_projects()
        self._rebuild_recent_menu()

    def _on_open_log_folder(self) -> None:
        log_dir = paths.log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir)))

    def _on_about(self) -> None:
        show_about_dialog(self)

    # recent projects submenu

    def _remember(self, path: Path) -> None:
        self._settings.add_recent_project(str(path))
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self) -> None:
        menu = self.ui.recent_projects_menu
        menu.clear()
        recent = self._settings.recent_projects
        if not recent:
            empty = menu.addAction("No recent projects")
            empty.setEnabled(False)
        else:
            for path_str in recent:
                action = QAction(paths.contract_user_path(path_str), self)
                action.triggered.connect(lambda _checked=False, p=path_str: self._open_recent(p))
                menu.addAction(action)
        menu.addSeparator()
        self.ui.action_clear_recent.setEnabled(bool(recent))
        menu.addAction(self.ui.action_clear_recent)
        self._welcome_panel.set_recent_projects(recent)

    def _open_recent(self, path_str: str) -> None:
        path = Path(path_str)
        if path.is_dir() and paths.project_toml(path).exists():
            self._load_and_remember(path)
            return
        if path.is_file() and path.suffix == ".pamproj":
            # A pre-upgrade recent entry pointing at a legacy project file.
            self._migrate_legacy(path)
            return
        QMessageBox.warning(
            self,
            "Project not found",
            f"The project is no longer accessible:\n{path}",
        )

    # state reactions

    def _on_project_changed(self, project: object) -> None:
        self._refresh_action_state(project)
        if project is None:
            self.setWindowTitle("PAM Analyzer")
            self._show_welcome()
        else:
            name = project.name  # type: ignore[attr-defined]
            self.setWindowTitle(f"PAM Analyzer - {name}")
            self._show_tabs()

    def _show_welcome(self) -> None:
        self._welcome_panel.set_recent_projects(self._settings.recent_projects)
        self.ui.content_stack.setCurrentWidget(self.ui.welcome_page)

    def _show_tabs(self) -> None:
        self.ui.content_stack.setCurrentWidget(self.ui.tabs_page)
        # Always land on the Project tab when a new project loads.
        self.ui.tab_widget.setCurrentIndex(self.ui.tab_widget.indexOf(self._project_panel))

    def _refresh_action_state(self, project: object) -> None:
        self.ui.action_close.setEnabled(project is not None)

    # geometry / close

    def _restore_geometry(self) -> None:
        geom = self._settings.window_geometry
        if geom is not None:
            self.restoreGeometry(geom)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802  Qt API
        self._birdnet_panel.request_shutdown()
        self._campaigns_panel.request_shutdown()
        self._settings.window_geometry = self.saveGeometry()
        super().closeEvent(event)

    def _confirm_cancel_running(self) -> bool:
        busy = [
            label
            for panel in (self._birdnet_panel, self._campaigns_panel)
            if (label := panel.busy_label())
        ]
        if not busy:
            return True
        if len(busy) == 1:
            msg = f"A {busy[0]} is running. Switching projects will stop it."
        else:
            joined = ", ".join(busy)
            msg = f"Background tasks are running ({joined}). Switching projects will stop them."
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Switch projects?")
        box.setText(msg)
        switch_btn = box.addButton("Switch projects", QMessageBox.ButtonRole.DestructiveRole)
        keep_btn = box.addButton("Keep running", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep_btn)
        box.exec()
        if box.clickedButton() is not switch_btn:
            return False
        self._cancel_all_workers()
        return True

    def _cancel_all_workers(self) -> None:
        self.ui.status_bar.showMessage("Cancelling background tasks…", 0)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._birdnet_panel.request_shutdown()
            self._campaigns_panel.request_shutdown()
        finally:
            QApplication.restoreOverrideCursor()
            self.ui.status_bar.clearMessage()

    # splash screen

    def showEvent(self, event: QEvent) -> None:  # noqa: N802  Qt API
        """Close the PyInstaller splash screen once the main window is shown."""
        super().showEvent(event)
        if not self._splash_closed:
            self._splash_closed = True
            self._close_splash()

    @staticmethod
    def _close_splash() -> None:
        """Close the PyInstaller splash screen if it exists."""
        try:
            import pyi_splash  # type: ignore[import]  # only exists in PyInstaller builds

            pyi_splash.close()
        except ImportError:
            pass
