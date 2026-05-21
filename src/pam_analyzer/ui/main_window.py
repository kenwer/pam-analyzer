import sys
from pathlib import Path

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QAction, QCloseEvent, QColor
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from ..app.settings import AppSettings
from ..infrastructure import (
    BirdnetAnalyzerRunner,
    CsvDetectionRepository,
    SoundfileAudioExtractor,
    TomlCampaignRepository,
)
from ..workers import ImportOrchestrator
from .app_state import AppState
from .dialogs.about_dialog import show_about_dialog
from .panels.birdnet_panel import BirdNetPanel
from .panels.campaigns_panel import CampaignsPanel
from .panels.examine_panel import ExaminePanel
from .panels.project_panel import ProjectPanel
from .panels.welcome_panel import WelcomePanel
from .ui_main_window import Ui_MainWindow


class MainWindow(QMainWindow):
    def __init__(
        self,
        app_state: AppState,
        campaign_repo: TomlCampaignRepository,
        detections_repo: CsvDetectionRepository,
        analysis_runner: BirdnetAnalyzerRunner,
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

        self._birdnet_panel = BirdNetPanel(app_state, analysis_runner, campaign_repo, self.ui.birdnet_tab)
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
        self.ui.action_save.triggered.connect(self._app_state.save_project)
        self.ui.action_save_as.triggered.connect(self._on_save_as)
        self.ui.action_close.triggered.connect(self._on_close_project)
        self.ui.action_clear_recent.triggered.connect(self._on_clear_recent)
        self.ui.action_quit.triggered.connect(self.close)
        self.ui.action_about.triggered.connect(self._on_about)

    def _wire_state(self) -> None:
        self._app_state.statusMessage.connect(lambda msg: self.ui.status_bar.showMessage(msg, 5000))
        self._app_state.errorOccurred.connect(lambda msg: QMessageBox.warning(self, "PAM Analyzer", msg))
        self._app_state.projectChanged.connect(self._on_project_changed)
        self._app_state.projectDirtyChanged.connect(self._on_dirty_changed)
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

    def _on_import_started(self, campaign_name: str) -> None:
        self._import_running = True
        self._update_tab_lock()
        self.ui.status_bar.showMessage(f"Watching for SD cards (campaign: {campaign_name})…", 0)

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
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "New project",
            self._settings.last_directory,
            "PAM Analyzer projects (*.pamproj)",
        )
        if not path_str:
            return
        if not self._confirm_cancel_running():
            return
        path = Path(path_str)
        if path.suffix != ".pamproj":
            path = path.with_suffix(".pamproj")
        self._app_state.create_project(path)
        if self._app_state.project is not None:
            self._remember(path)

    def _on_open(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Open project",
            self._settings.last_directory,
            "PAM Analyzer projects (*.pamproj)",
        )
        if not path_str:
            return
        if not self._confirm_cancel_running():
            return
        path = Path(path_str)
        self._app_state.load_project(path)
        if self._app_state.project is not None:
            self._remember(path)

    def _on_save_as(self) -> None:
        if self._app_state.project is None:
            return
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Save project as",
            str(self._app_state.project.path),
            "PAM Analyzer projects (*.pamproj)",
        )
        if not path_str:
            return
        path = Path(path_str)
        if path.suffix != ".pamproj":
            path = path.with_suffix(".pamproj")
        self._app_state.save_project_as(path)
        if self._app_state.project is not None:
            self._remember(path)

    def _on_close_project(self) -> None:
        if not self._confirm_discard_dirty():
            return
        if not self._confirm_cancel_running():
            return
        self._app_state.close_project()

    def _on_clear_recent(self) -> None:
        self._settings.clear_recent_projects()
        self._rebuild_recent_menu()

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
                action = QAction(self._display_path(path_str), self)
                action.triggered.connect(lambda _checked=False, p=path_str: self._open_recent(p))
                menu.addAction(action)
        menu.addSeparator()
        self.ui.action_clear_recent.setEnabled(bool(recent))
        menu.addAction(self.ui.action_clear_recent)
        self._welcome_panel.set_recent_projects(recent)

    def _open_recent(self, path_str: str) -> None:
        path = Path(path_str)
        if not path.exists():
            QMessageBox.warning(
                self,
                "Project not found",
                f"The project file is no longer accessible:\n{path}",
            )
            return
        if not self._confirm_cancel_running():
            return
        self._app_state.load_project(path)
        if self._app_state.project is not None:
            self._remember(path)

    @staticmethod
    def _display_path(path_str: str) -> str:
        try:
            return f"~/{Path(path_str).relative_to(Path.home())}"
        except ValueError:
            return path_str

    # state reactions

    def _on_project_changed(self, project: object) -> None:
        self._refresh_action_state(project)
        if project is None:
            self.setWindowTitle("PAM Analyzer")
            self._show_welcome()
        else:
            name = project.path.name  # type: ignore[attr-defined]
            dirty = "*" if self._app_state.is_dirty else ""
            self.setWindowTitle(f"PAM Analyzer - {name}{dirty}")
            self._show_tabs()

    def _show_welcome(self) -> None:
        self._welcome_panel.set_recent_projects(self._settings.recent_projects)
        self.ui.content_stack.setCurrentWidget(self.ui.welcome_page)

    def _show_tabs(self) -> None:
        self.ui.content_stack.setCurrentWidget(self.ui.tabs_page)
        # Always land on the Project tab when a new project loads.
        self.ui.tab_widget.setCurrentIndex(self.ui.tab_widget.indexOf(self._project_panel))

    def _on_dirty_changed(self, dirty: bool) -> None:
        project = self._app_state.project
        if project is None:
            return
        marker = "*" if dirty else ""
        self.setWindowTitle(f"PAM Analyzer - {project.path.name}{marker}")
        self.ui.action_save.setEnabled(dirty)

    def _refresh_action_state(self, project: object) -> None:
        loaded = project is not None
        self.ui.action_save.setEnabled(loaded and self._app_state.is_dirty)
        self.ui.action_save_as.setEnabled(loaded)
        self.ui.action_close.setEnabled(loaded)

    # geometry / close

    def _restore_geometry(self) -> None:
        geom = self._settings.window_geometry
        if geom is not None:
            self.restoreGeometry(geom)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802  Qt API
        if not self._confirm_discard_dirty():
            event.ignore()
            return
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

    def _confirm_discard_dirty(self) -> bool:
        if not self._app_state.is_dirty:
            return True
        choice = QMessageBox.question(
            self,
            "Unsaved changes",
            "The current project has unsaved changes. Save before continuing?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice == QMessageBox.StandardButton.Cancel:
            return False
        if choice == QMessageBox.StandardButton.Save:
            self._app_state.save_project()
        return True

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
