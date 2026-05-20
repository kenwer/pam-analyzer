"""BirdNET panel: configure analysis settings, run, and review results."""

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QMenu,
    QMessageBox,
    QToolButton,
    QWidget,
    QWidgetAction,
)

from ...domain import (
    AnalysisProgressSnapshot,
    AnalysisRunner,
    AnalysisRunResult,
    AnalysisSettings,
    Campaign,
    FilterMode,
    Project,
)
from ...infrastructure import TomlCampaignRepository
from ...workers import AnalysisWorker
from ..app_state import AppState
from ..models.birdnet_results_model import BirdnetResultsModel
from .ui_birdnet_panel import Ui_BirdNetPanel

_ALL_CAMPAIGNS_LABEL = "All campaigns"
_ALL_CAMPAIGNS_DATA = "__all__"


class _StatusPage(IntEnum):
    IDLE = 0
    PROGRESS = 1
    RESULTS = 2


@dataclass
class _PanelState:
    available_locales: list[str] = field(default_factory=list)
    running: bool = False
    last_result: AnalysisRunResult | None = None


class BirdNetPanel(QWidget):
    def __init__(
        self,
        app_state: AppState,
        runner: AnalysisRunner,
        campaign_repo: TomlCampaignRepository,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ui = Ui_BirdNetPanel()
        self.ui.setupUi(self)

        self._app_state = app_state
        self._runner = runner
        self._campaign_repo = campaign_repo
        self._state = _PanelState()
        self._thread: QThread | None = None
        self._worker: AnalysisWorker | None = None
        self._results_model = BirdnetResultsModel(self)
        self.ui.results_tree.setModel(self._results_model)
        self.ui.results_tree.header().setStretchLastSection(True)

        self._locale_checks: dict[str, QCheckBox] = {}
        self._state.available_locales = self._runner.available_locales()
        self._build_locales_menu()
        self._wire_signals()
        self._set_status_page(_StatusPage.IDLE)
        self._render_project(app_state.project)

    def _wire_signals(self) -> None:
        self._app_state.projectChanged.connect(self._render_project)
        self._app_state.campaignsChanged.connect(self._rebuild_campaign_combo)

        self.ui.campaign_combo.currentIndexChanged.connect(self._on_campaign_changed)
        self.ui.min_conf_slider.valueChanged.connect(self._on_min_conf_changed)
        self.ui.overlap_slider.valueChanged.connect(self._on_overlap_changed)
        self.ui.run_button.clicked.connect(self._on_run_clicked)

        self.ui.output_path_label.linkActivated.connect(lambda _link: self._open_path(self._current_output_dir()))

    def _build_locales_menu(self) -> None:
        menu = QMenu(self.ui.locales_button)
        self._locale_checks = {}
        for loc in sorted({"en", *self._state.available_locales}):
            chk = QCheckBox(loc, menu)
            chk.toggled.connect(self._on_locales_changed)
            self._locale_checks[loc] = chk
            action = QWidgetAction(menu)
            action.setDefaultWidget(chk)
            menu.addAction(action)
        self.ui.locales_button.setMenu(menu)

    def _render_project(self, project: Project | None) -> None:
        loaded = project is not None
        self._set_settings_enabled(loaded)
        if not loaded:
            self.ui.campaign_combo.clear()
            self.ui.filter_info_label.clear()
            self._set_status_page(_StatusPage.IDLE)
            return
        assert project is not None
        self._set_slider_values(project)
        self._set_locale_checks(project.birdnet_locales)
        self._rebuild_campaign_combo(self._app_state.campaigns)

    def _set_settings_enabled(self, enabled: bool) -> None:
        for w in (
            self.ui.campaign_combo,
            self.ui.min_conf_slider,
            self.ui.overlap_slider,
            self.ui.locales_button,
            self.ui.run_button,
        ):
            w.setEnabled(enabled)

    def _set_slider_values(self, project: Project) -> None:
        for slider, value in (
            (self.ui.min_conf_slider, int(round(project.birdnet_min_conf * 100))),
            (self.ui.overlap_slider, int(round(project.birdnet_overlap * 10))),
        ):
            slider.blockSignals(True)
            try:
                slider.setValue(value)
            finally:
                slider.blockSignals(False)
        self._refresh_slider_labels()

    def _refresh_slider_labels(self) -> None:
        self.ui.min_conf_value.setText(f"{self.ui.min_conf_slider.value() / 100:.2f}")
        self.ui.overlap_value.setText(f"{self.ui.overlap_slider.value() / 10:.1f}")

    def _set_locale_checks(self, selected: tuple[str, ...]) -> None:
        chosen = set(selected)
        for loc, chk in self._locale_checks.items():
            chk.blockSignals(True)
            try:
                chk.setChecked(loc in chosen)
            finally:
                chk.blockSignals(False)
        self._refresh_locales_button_label()

    def _refresh_locales_button_label(self) -> None:
        n = sum(1 for c in self._locale_checks.values() if c.isChecked())
        self.ui.locales_button.setText("Languages" if n == 0 else f"Languages ({n})")

    def _rebuild_campaign_combo(self, campaigns: list[Campaign]) -> None:
        combo = self.ui.campaign_combo
        combo.blockSignals(True)
        combo.clear()
        if campaigns:
            combo.addItem(
                f"{_ALL_CAMPAIGNS_LABEL} ({len(campaigns)})",
                _ALL_CAMPAIGNS_DATA,
            )
            for c in campaigns:
                combo.addItem(c.name, c.name)
        combo.blockSignals(False)
        if combo.count() > 0:
            combo.setCurrentIndex(0)
            self._on_campaign_changed(0)
        else:
            self.ui.filter_info_label.setText("No campaigns found")
            self.ui.run_button.setEnabled(False)

    def _on_campaign_changed(self, index: int) -> None:
        if index < 0:
            return
        data = self.ui.campaign_combo.itemData(index)
        if data == _ALL_CAMPAIGNS_DATA:
            n = len(self._app_state.campaigns)
            self.ui.filter_info_label.setText(f"{n} campaign{'s' if n != 1 else ''}")
        else:
            campaign = self._campaign_by_name(str(data))
            self.ui.filter_info_label.setText(self._campaign_info_text(campaign))
        self.ui.run_button.setEnabled(self._can_run())

    def _campaign_by_name(self, name: str) -> Campaign | None:
        for c in self._app_state.campaigns:
            if c.name == name:
                return c
        return None

    def _campaign_info_text(self, campaign: Campaign | None) -> str:
        if campaign is None:
            return ""
        if campaign.species_filter_mode == FilterMode.LOCATION and campaign.location:
            loc = campaign.location
            ns = "N" if loc.latitude >= 0 else "S"
            ew = "E" if loc.longitude >= 0 else "W"
            return f"● Location  {abs(loc.latitude):.2f}°{ns}, {abs(loc.longitude):.2f}°{ew}"
        return "● Species list"

    def _can_run(self) -> bool:
        if self._state.running or self._app_state.project is None:
            return False
        return self.ui.campaign_combo.count() > 0

    def _on_min_conf_changed(self, value: int) -> None:
        self._refresh_slider_labels()
        self._app_state.update_birdnet_settings(min_conf=value / 100.0)

    def _on_overlap_changed(self, value: int) -> None:
        self._refresh_slider_labels()
        self._app_state.update_birdnet_settings(overlap=value / 10.0)

    def _on_locales_changed(self, _checked: bool) -> None:
        self._refresh_locales_button_label()
        locales = tuple(loc for loc, chk in self._locale_checks.items() if chk.isChecked())
        self._app_state.update_birdnet_settings(locales=locales)

    def _on_run_clicked(self) -> None:
        if self._state.running:
            self._request_cancel()
            return
        self._start_run()

    def _start_run(self) -> None:
        project = self._app_state.project
        if project is None:
            return
        selected_data = self.ui.campaign_combo.currentData()
        if selected_data == _ALL_CAMPAIGNS_DATA:
            campaigns = list(self._app_state.campaigns)
        else:
            c = self._campaign_by_name(str(selected_data))
            if c is None:
                return
            campaigns = [c]
        if not campaigns:
            QMessageBox.information(self, "BirdNET", "No campaigns to run.")
            return

        settings = AnalysisSettings(
            min_conf=project.birdnet_min_conf,
            overlap=project.birdnet_overlap,
            locales=project.birdnet_locales,
        )

        self._thread = QThread(self)
        self._worker = AnalysisWorker(self._runner, self._campaign_repo, project, campaigns, settings)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.succeeded.connect(self._on_succeeded)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        # DirectConnection: quit() is called inline from the worker thread.
        # Without this, quit() is queued to the main thread, which is already
        # blocked in _teardown_worker's thread.wait(), causing a deadlock.
        for sig in (
            self._worker.succeeded,
            self._worker.failed,
            self._worker.cancelled,
        ):
            sig.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)

        self._state.running = True
        self._set_status_page(_StatusPage.PROGRESS)
        self.ui.progress_bar.setRange(0, 0)  # indeterminate while preparing
        self.ui.progress_label.setText("Preparing…")
        self.ui.run_button.setText("Stop")
        self._set_settings_enabled(False)
        self.ui.run_button.setEnabled(True)  # keep Stop enabled
        self._app_state.analysisStarted.emit()
        self._thread.start()

    def _request_cancel(self) -> None:
        if self._worker is not None:
            self._worker.request_cancel()
        self.ui.progress_label.setText("Cancelling… (waiting for current file)")
        self.ui.run_button.setEnabled(False)

    def request_shutdown(self) -> None:
        """Called from closeEvent: cancel any running analysis and wait."""
        if self._state.running:
            self._request_cancel()
        if self._thread is not None:
            self._thread.quit()  # safe to call from main thread; stops the event loop
            self._thread.wait(5000)

    def _on_progress(self, snap: AnalysisProgressSnapshot) -> None:
        self._app_state.analysisProgress.emit(snap)
        if snap.files_total > 0:
            self.ui.progress_bar.setRange(0, snap.files_total)
            self.ui.progress_bar.setValue(min(snap.files_done, snap.files_total))
        else:
            self.ui.progress_bar.setRange(0, 0)
        prefix = (
            f"Campaign {snap.campaign_index}/{snap.total_campaigns}: {snap.campaign}"
            if snap.total_campaigns > 1
            else snap.campaign
        )
        parts = [prefix, snap.phase]
        if snap.files_total > 0 and snap.phase == "analyzing":
            parts.append(f"{snap.files_done}/{snap.files_total}")
        if snap.phase_detail:
            parts.append(snap.phase_detail)
        self.ui.progress_label.setText("  ·  ".join(parts))

    def _on_succeeded(self, result: AnalysisRunResult) -> None:
        self._teardown_worker()
        self._state.last_result = result
        self._app_state.analysisFinished.emit(result)
        self._render_results(result)
        self._set_status_page(_StatusPage.RESULTS)

    def _on_failed(self, message: str) -> None:
        self._teardown_worker()
        self._app_state.analysisFinished.emit(None)
        self._app_state.errorOccurred.emit(f"Analysis failed: {message}")
        self._set_status_page(_StatusPage.IDLE)

    def _on_cancelled(self) -> None:
        self._teardown_worker()
        self._app_state.analysisFinished.emit(None)
        self._app_state.statusMessage.emit("Analysis cancelled.")
        self._set_status_page(_StatusPage.IDLE)

    def _teardown_worker(self) -> None:
        self._state.running = False
        if self._thread is not None:
            # wait() is safe here: quit() was already called via DirectConnection
            # from the worker thread, so the event loop stops without needing the
            # main thread to process it. wait() returns once the thread truly exits.
            self._thread.wait()
            self._thread.deleteLater()
            self._thread = None
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        self.ui.run_button.setText("Run BirdNET")
        self._set_settings_enabled(True)
        self.ui.run_button.setEnabled(self._can_run())

    def _render_results(self, result: AnalysisRunResult) -> None:
        self._results_model.set_result(result)
        self.ui.results_tree.expandAll()
        self._populate_row_widgets(result)
        self.ui.summary_label.setText(self._build_summary(result))
        out_dir = self._current_output_dir()
        if out_dir is not None:
            self.ui.output_path_label.setText(f'Output: <a href="open">{out_dir}</a>')
        else:
            self.ui.output_path_label.clear()

    def _populate_row_widgets(self, result: AnalysisRunResult) -> None:
        """Attach file-button rows to column 1 of each tree row."""

        def files_widget(folder: Path, files: list[Path]) -> QWidget:
            container = QWidget(self.ui.results_tree)
            layout = QHBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(2)
            folder_btn = QToolButton(container)
            folder_btn.setText("📁")
            folder_btn.setToolTip(f"Open folder: {folder}")
            folder_btn.setAutoRaise(True)
            folder_btn.clicked.connect(lambda _=False, p=folder: self._open_path(p))
            layout.addWidget(folder_btn)
            for f in files:
                btn = QToolButton(container)
                btn.setText(f.name)
                btn.setAutoRaise(True)
                btn.setToolTip(str(f))
                btn.setEnabled(f.exists())
                btn.clicked.connect(lambda _=False, p=f: self._open_path(p))
                layout.addWidget(btn)
            layout.addStretch(1)
            return container

        for index, folder, files in self._results_model.iter_file_rows():
            self.ui.results_tree.setIndexWidget(index, files_widget(folder, files))
        self.ui.results_tree.resizeColumnToContents(0)

    def _build_summary(self, result: AnalysisRunResult) -> str:
        total_det = sum(c.detection_count for c in result.campaigns)
        total_wav = sum(c.wav_count for c in result.campaigns)
        total_aru = sum(c.aru_count for c in result.campaigns)
        weeks = sum(len(c.week_results) for c in result.campaigns)
        m, s = divmod(int(result.elapsed), 60)
        dur = f"{m}m {s:02d}s" if m else f"{s}s"
        parts = [f"{total_det:,} detections"]
        if len(result.campaigns) > 1:
            parts.append(f"{len(result.campaigns)} campaigns")
        if total_aru:
            parts.append(f"{total_aru} ARUs")
        if weeks:
            parts.append(f"{weeks} weeks")
        parts.append(f"{total_wav} files")
        parts.append(dur)
        return "✓ " + "  ·  ".join(parts)

    def _current_output_dir(self) -> Path | None:
        if self._app_state.project is None:
            return None
        return self._app_state.project.output_base

    def _open_path(self, path: Path | None) -> None:
        if path is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _set_status_page(self, page: _StatusPage) -> None:
        self.ui.status_stack.setCurrentIndex(page)
