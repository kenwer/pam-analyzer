"""BirdNET panel: configure analysis settings, run, and review results."""

from collections import Counter
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt, QThread, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMessageBox,
    QToolButton,
    QWidget,
)

from ...domain import (
    AnalysisInventory,
    AnalysisProgressSnapshot,
    AnalysisRunner,
    AnalysisRunResult,
    Campaign,
    FilterMode,
    Project,
    RunStatus,
)
from ...widgets.no_hover_style import disable_item_hover
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
    running: bool = False


class BirdNetPanel(QWidget):
    def __init__(
        self,
        app_state: AppState,
        runner: AnalysisRunner,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ui = Ui_BirdNetPanel()
        self.ui.setupUi(self)

        self._app_state = app_state
        self._runner: AnalysisRunner = runner
        self._runner_key: str = runner.model_key
        self._state = _PanelState()
        self._thread: QThread | None = None
        self._worker: AnalysisWorker | None = None
        self._results_model = BirdnetResultsModel(self)
        self.ui.results_tree.setModel(self._results_model)
        self.ui.results_tree.header().setStretchLastSection(True)
        disable_item_hover(self.ui.results_tree)

        self._update_run_label()
        self._wire_signals()
        self._set_status_page(_StatusPage.IDLE)
        self._render_project(app_state.project)
        self._on_analysis_inventory_changed(app_state.analysis_inventory)

    def _update_run_label(self) -> None:
        """Show the selected model on the Run button (idle state only).

        During a run the button shows Stop, so leave it alone.
        """
        if not self._state.running:
            self.ui.run_button.setText(self._idle_run_label())

    def _idle_run_label(self) -> str:
        return f"Run {self._runner_key}"

    def _busy_run_label(self) -> str:
        return f"Stop {self._runner_key}"

    def _wire_signals(self) -> None:
        self._app_state.projectChanged.connect(self._render_project)
        self._app_state.campaignsChanged.connect(self._rebuild_campaign_combo)
        self._app_state.analysisInventoryChanged.connect(self._on_analysis_inventory_changed)

        self.ui.campaign_combo.currentIndexChanged.connect(self._on_campaign_changed)
        self.ui.run_button.clicked.connect(self._on_run_clicked)

    def _render_project(self, project: Project | None) -> None:
        loaded = project is not None
        self._set_settings_enabled(loaded)
        if not loaded:
            self.ui.campaign_combo.clear()
            self.ui.filter_info_label.clear()
            self._set_status_page(_StatusPage.IDLE)
            return
        assert project is not None
        self._rebuild_campaign_combo(self._app_state.campaigns)

    def _set_settings_enabled(self, enabled: bool) -> None:
        for w in (
            self.ui.campaign_combo,
            self.ui.run_button,
        ):
            w.setEnabled(enabled)

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
            QMessageBox.information(self, self._runner_key, "No campaigns to run.")
            return

        settings = project.analysis_settings

        self._thread = QThread(self)
        self._worker = AnalysisWorker(self._runner, project, campaigns, settings)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        # DirectConnection: quit() is called inline from the worker thread.
        # Without this, quit() is queued to the main thread, which is already
        # blocked in _teardown_worker's thread.wait(), causing a deadlock.
        self._worker.finished.connect(
            self._thread.quit, Qt.ConnectionType.DirectConnection
        )

        self._state.running = True
        self._set_status_page(_StatusPage.PROGRESS)
        self.ui.progress_bar.setRange(0, 0)  # indeterminate while preparing
        self.ui.progress_label.setText("Preparing…")
        self.ui.run_button.setText(self._busy_run_label())
        self.ui.run_button.setChecked(True)
        self._set_settings_enabled(False)
        self.ui.run_button.setEnabled(True)  # keep Stop enabled
        self._app_state.analysisStarted.emit(self._runner_key)
        self._thread.start()

    def _request_cancel(self) -> None:
        if self._worker is not None:
            self._worker.request_cancel()
        self.ui.progress_label.setText("Cancelling… (waiting for current file)")
        self.ui.run_button.setEnabled(False)

    def request_shutdown(self) -> None:
        """Cancel any running analysis and wait. Called on app close and on
        project switch. processEvents() drains the worker's queued
        succeeded/failed/cancelled signal so it's handled while the panel is
        still in its current session, instead of leaking into the next one.
        """
        if self._state.running:
            self._request_cancel()
        if self._thread is not None:
            self._thread.quit()  # safe to call from main thread; stops the event loop
            self._thread.wait(5000)
            QCoreApplication.processEvents()

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
        parts = [p for p in (prefix, snap.phase) if p]
        if snap.files_total > 0 and snap.phase == "analyzing":
            parts.append(f"{snap.files_done}/{snap.files_total}")
        if snap.phase_detail:
            parts.append(snap.phase_detail)
        self.ui.progress_label.setText("  ·  ".join(parts))

    def _on_finished(self, outcome: AnalysisRunResult) -> None:
        """Single terminal handler for every way a run can end.

        The disk is the source of truth for what was produced: each finished
        campaign writes its CSV before the run moves on, so a fresh discovery
        surfaces the completed work whether the run completed, was cancelled,
        or failed partway. refresh_analysis_inventory() drives the
        results page through analysisInventoryChanged. This handler only adds
        the status-specific message on top, and falls back to the idle page
        when nothing was produced.
        """
        self._teardown_worker()
        self._app_state.analysisFinished.emit(outcome)
        self._app_state.refresh_analysis_inventory()
        # When the discovery snapshot did not change (a cancel before any CSV
        # was written, over a project that had none to begin with), the signal
        # stays silent, so the progress page would otherwise linger.
        results = self._app_state.analysis_inventory
        if results is None or not results.campaigns:
            self._set_status_page(_StatusPage.IDLE)
        if outcome.status is RunStatus.FAILED:
            self._app_state.errorOccurred.emit(
                f"Analysis failed: {outcome.error or 'unknown error'}"
            )
        elif outcome.status is RunStatus.CANCELLED:
            self._app_state.statusMessage.emit("Analysis cancelled.")

    def _on_analysis_inventory_changed(self, results: AnalysisInventory | None) -> None:
        # A run currently in progress owns the status page; don't fight it.
        if self._state.running:
            return
        if results is None or not results.campaigns:
            self._results_model.clear()
            self._results_model.setHorizontalHeaderLabels(["Name", "Files"])
            self.ui.summary_label.clear()
            self._set_status_page(_StatusPage.IDLE)
        else:
            self._render_results(results)
            self._set_status_page(_StatusPage.RESULTS)

    def is_busy(self) -> bool:
        return self._state.running

    def busy_label(self) -> str | None:
        return f"{self._runner_key} analysis" if self._state.running else None

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
        self.ui.run_button.setText(self._idle_run_label())
        self.ui.run_button.setChecked(False)
        self._set_settings_enabled(True)
        self.ui.run_button.setEnabled(self._can_run())

    def _render_results(self, result: AnalysisInventory) -> None:
        self._results_model.set_result(result)
        self.ui.results_tree.expandAll()
        self._populate_row_widgets()
        self.ui.summary_label.setText(self._build_summary(result))

    def _populate_row_widgets(self) -> None:
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
                if not f.exists():
                    continue
                btn = QToolButton(container)
                btn.setText(f.name)
                btn.setAutoRaise(True)
                btn.setToolTip(str(f))
                btn.clicked.connect(lambda _=False, p=f: self._open_path(p))
                layout.addWidget(btn)
            layout.addStretch(1)
            return container

        for index, folder, files in self._results_model.iter_file_rows():
            self.ui.results_tree.setIndexWidget(index, files_widget(folder, files))
        self.ui.results_tree.resizeColumnToContents(0)

    def _build_summary(self, result: AnalysisInventory) -> str:
        total_det = sum(c.detection_count for c in result.campaigns)
        parts = [f"{total_det:,} detections"]
        if len(result.campaigns) > 1:
            parts.append(f"{len(result.campaigns)} CSVs")
        return "  ·  ".join(parts) + self._model_breakdown(result)

    def _model_breakdown(self, result: AnalysisInventory) -> str:
        """Per-model detections and CSV counts, appended when the results span
        more than one model (e.g. a campaign analyzed by both BirdNET and
        Perch). Suppressed for a single model, where each cell would just
        repeat the headline totals. Ordered by detection count, highest first.

        One CSV is written per campaign per model, so a model's CSV count is
        just how many campaign results carry its model_key.
        """
        det_by_model: Counter[str] = Counter()
        csv_by_model: Counter[str] = Counter()
        for c in result.campaigns:
            det_by_model[c.model_key] += c.detection_count
            csv_by_model[c.model_key] += 1
        if len(det_by_model) < 2:
            return ""
        order = sorted(det_by_model, key=lambda m: (-det_by_model[m], m))
        cells = "]   [".join(
            f"{m or 'unknown'}: {_plural(det_by_model[m], 'detection')}, {_plural(csv_by_model[m], 'CSV')}"
            for m in order
        )
        return f"        [{cells}]"

    def _open_path(self, path: Path | None) -> None:
        if path is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _set_status_page(self, page: _StatusPage) -> None:
        self.ui.status_stack.setCurrentIndex(page)


def _plural(n: int, noun: str) -> str:
    """Format a count with its noun, adding a trailing 's' unless n is 1."""
    return f"{n:,} {noun}" if n == 1 else f"{n:,} {noun}s"
