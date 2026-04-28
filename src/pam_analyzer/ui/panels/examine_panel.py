"""Examine panel: review detections in a multi-column-sort table."""

import csv
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from ...app.settings import AppSettings
from ...domain import Campaign, Detection, filter_top_per_aru_species
from ...infrastructure import CsvDetectionRepository, SoundfileAudioExtractor
from ..app_state import AppState
from ..models.detections_table_model import COLUMN_GETTERS, COLUMNS_BY_NAME, DetectionsTableModel
from .ui_examine_panel import Ui_ExaminePanel

_ALL_CAMPAIGNS_LABEL = "All campaigns"
_ALL_CAMPAIGNS_DATA = "__all__"

# Debounce window for the auto-save triggered from model.dataChanged.
# A short window groups bursts of edits (e.g. tab-through Verified cells)
# into a single CSV write without making the user wait noticeably.
_AUTOSAVE_DEBOUNCE_MS = 500


class ExaminePanel(QWidget):
    def __init__(
        self,
        app_state: AppState,
        detections_repo: CsvDetectionRepository,
        settings: AppSettings,
        audio_extractor: SoundfileAudioExtractor,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ui = Ui_ExaminePanel()
        self.ui.setupUi(self)

        self._app_state = app_state
        self._service = detections_repo
        self._settings = settings
        self._audio_extractor = audio_extractor
        self._model = DetectionsTableModel(self)
        self._raw_detections: list[Detection] = []  # full unfiltered list for current campaign

        self.ui.detections_table.setModel(self._model)
        self.ui.detections_table.setSortPriority([(COLUMNS_BY_NAME["Confidence"], Qt.DescendingOrder)])
        # The Confidence/numeric columns sort fastest via the model's fast-path.

        # Restore hidden-column state from QSettings before any data loads, so
        # the first fitColumnsToContents skips hidden columns.
        self.ui.detections_table.setHiddenColumnNames(self._settings.examine_hidden_columns)

        # Wider glyphs need a bit more breathing room than the default tool
        # button width. Apply once for all three icon buttons.
        for btn in (
            self.ui.padding_button,
            self.ui.columns_button,
            self.ui.export_button,
        ):
            btn.setMinimumWidth(28)

        self._build_padding_menu()
        self._build_columns_menu()
        self._build_export_menu()

        # Debounced auto-save. A single shot timer groups dataChanged bursts
        # so e.g. tabbing across several Verified cells results in one save.
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(_AUTOSAVE_DEBOUNCE_MS)
        self._autosave_timer.timeout.connect(self._flush_autosave)

        self._set_controls_enabled(False)
        self._wire_signals()

    def _wire_signals(self) -> None:
        self._app_state.projectChanged.connect(self._on_project_changed)
        self._app_state.campaignsChanged.connect(self._on_campaigns_changed)

        self.ui.campaign_combo.currentIndexChanged.connect(self._on_campaign_selected)
        self.ui.max_per_spin.valueChanged.connect(self._apply_filter)
        self.pad_before_spin.valueChanged.connect(self._on_padding_changed)
        self.pad_after_spin.valueChanged.connect(self._on_padding_changed)
        self.ui.detections_table.columnVisibilityChanged.connect(self._on_column_visibility_changed)
        self.ui.detections_table.statusChanged.connect(self._on_detection_count_changed)
        self._model.dataChanged.connect(self._schedule_autosave)

    # popups
    def _build_padding_menu(self) -> None:
        """QMenu wrapping a small QFormLayout popup for the padding spinboxes."""
        popup = QWidget()
        outer = QVBoxLayout(popup)
        outer.setContentsMargins(8, 6, 8, 8)
        outer.setSpacing(4)

        title = QLabel("Playback padding", popup)
        title_font = title.font()
        title_font.setBold(True)
        title.setFont(title_font)
        outer.addWidget(title)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(4)
        self.pad_before_spin = self._make_padding_spin(popup)
        self.pad_after_spin = self._make_padding_spin(popup)
        form.addRow("Before (s)", self.pad_before_spin)
        form.addRow("After (s)", self.pad_after_spin)
        outer.addLayout(form)

        menu = QMenu(self.ui.padding_button)
        action = QWidgetAction(menu)
        action.setDefaultWidget(popup)
        menu.addAction(action)
        self.ui.padding_button.setMenu(menu)

    @staticmethod
    def _make_padding_spin(parent: QWidget) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(parent)
        spin.setRange(0.0, 30.0)
        spin.setSingleStep(0.5)
        spin.setDecimals(1)
        return spin

    def _build_columns_menu(self) -> None:
        self.ui.columns_button.setMenu(self.ui.detections_table.makeColumnsMenu(self.ui.columns_button))

    def _build_export_menu(self) -> None:
        menu = QMenu(self.ui.export_button)
        csv_action = QAction("Export CSV…", menu)
        csv_action.triggered.connect(self._on_export_csv_clicked)
        snip_action = QAction("Export audio snippets…", menu)
        snip_action.triggered.connect(self._on_export_snippets_clicked)
        menu.addAction(csv_action)
        menu.addAction(snip_action)
        self.ui.export_button.setMenu(menu)

    # state observers

    def _on_project_changed(self, project: object) -> None:
        # Flush any pending edits against the previous project before switching.
        if self._autosave_timer.isActive():
            self._autosave_timer.stop()
            self._flush_autosave()
        loaded = project is not None
        self._set_controls_enabled(loaded)
        audio_root = project.audio_recordings_path if loaded else None  # type: ignore[union-attr]
        self.ui.detections_table.setAudioRoot(audio_root)
        # Sync padding spinboxes + audio player from the new project.
        before = float(getattr(project, "snippet_padding_before", 0.0) or 0.0)
        after = float(getattr(project, "snippet_padding_after", 0.0) or 0.0)
        self._set_padding_widgets(before, after)
        self.ui.detections_table.setPlaybackPadding(before, after)

    def _on_campaigns_changed(self, campaigns: list[Campaign]) -> None:
        combo = self.ui.campaign_combo
        combo.blockSignals(True)
        combo.clear()
        if campaigns:
            combo.addItem(_ALL_CAMPAIGNS_LABEL, _ALL_CAMPAIGNS_DATA)
            for c in campaigns:
                combo.addItem(c.name, c.name)
        combo.blockSignals(False)
        if campaigns:
            self._on_campaign_selected(0)

    # handlers

    def _on_campaign_selected(self, index: int) -> None:
        project = self._app_state.project
        if project is None or index < 0:
            return
        data = self.ui.campaign_combo.itemData(index)
        try:
            if data == _ALL_CAMPAIGNS_DATA:
                self._raw_detections = self._service.load_combined(project.output_base, project.name)
            else:
                self._raw_detections = self._service.load_for_campaign(project.output_base, str(data))
        except Exception as exc:
            self._app_state.errorOccurred.emit(f"Failed to load detections: {exc}")
            self._raw_detections = []
        self._apply_filter()

    def _apply_filter(self) -> None:
        max_per = self.ui.max_per_spin.value()
        rows = filter_top_per_aru_species(self._raw_detections, max_per)
        self._model.set_detections(rows)
        # Refresh the Corrected_Species combo choices from the loaded data.
        self.ui.detections_table.setSpeciesChoices(sorted({d.species for d in self._raw_detections if d.species}))
        self.ui.detections_table.fitColumnsToContents()

    def _on_detection_count_changed(self, shown: int) -> None:
        total = len(self._raw_detections)
        self.ui.info_label.setText(f"{shown:,} / {total:,} detections")

    def _on_padding_changed(self) -> None:
        before = float(self.pad_before_spin.value())
        after = float(self.pad_after_spin.value())
        self.ui.detections_table.setPlaybackPadding(before, after)
        self._app_state.update_padding(before, after)

    def _on_column_visibility_changed(self, _col: int, _visible: bool) -> None:
        self._settings.examine_hidden_columns = self.ui.detections_table.hiddenColumnNames()

    def _schedule_autosave(self, *_: object) -> None:
        """Restart the debounce window. dataChanged emits a top-left/bottom-right
        pair plus an optional roles list. We don't care about the args here."""
        self._autosave_timer.start()

    def _flush_autosave(self) -> None:
        """Persist any dirty rows. Called when the debounce window expires or
        when the panel needs to release in-flight edits (project change, etc)."""
        project = self._app_state.project
        if project is None:
            self._model.take_dirty()  # discard, no project to save into
            return
        dirty = self._model.take_dirty()
        if not dirty:
            return
        # The repo overwrites each campaign's CSV with whatever it's handed,
        # so we must pass the FULL set (rows the user filtered out via
        # max-per or header filters live in self._raw_detections, and edits
        # propagate there because Detection objects are shared by reference).
        all_campaigns_loaded = self.ui.campaign_combo.currentData() == _ALL_CAMPAIGNS_DATA
        try:
            self._service.save(
                project.output_base,
                self._raw_detections,
                project_name=project.name,
                write_combined=all_campaigns_loaded,
            )
        except Exception as exc:
            self._app_state.errorOccurred.emit(f"Auto-save failed: {exc}")
            return
        self._app_state.statusMessage.emit(f"Saved {len(dirty)} edited detection(s).")

    # export

    def _on_export_csv_clicked(self) -> None:
        rows = self._model.detections()
        if not rows:
            QMessageBox.information(self, "Export CSV", "Nothing to export.")
            return
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export CSV",
            str(Path.home() / "detections.csv"),
            "CSV files (*.csv)",
        )
        if not path_str:
            return
        path = Path(path_str)
        if path.suffix.lower() != ".csv":
            path = path.with_suffix(".csv")
        try:
            _write_visible_csv(path, rows, self._visible_column_names())
        except Exception as exc:
            self._app_state.errorOccurred.emit(f"Export failed: {exc}")
            return
        self._app_state.statusMessage.emit(f"Exported {len(rows)} rows to {path.name}")

    def _on_export_snippets_clicked(self) -> None:
        project = self._app_state.project
        if project is None:
            return
        rows = self._model.detections()
        if not rows:
            QMessageBox.information(self, "Export snippets", "Nothing to export.")
            return
        folder_str = QFileDialog.getExistingDirectory(
            self,
            "Choose folder for audio snippets",
            str(Path.home()),
        )
        if not folder_str:
            return
        folder = Path(folder_str)
        audio_root = project.audio_recordings_path
        pad_before = float(project.snippet_padding_before or 0.0)
        pad_after = float(project.snippet_padding_after or 0.0)

        ok = 0
        errors: list[str] = []
        for d in rows:
            src = audio_root / d.file
            if not src.exists():
                errors.append(f"missing: {d.file}")
                continue
            start = max(0.0, d.start_time - pad_before)
            end = d.end_time + pad_after
            dst = folder / _snippet_filename(d, start, end)
            try:
                self._audio_extractor.extract(src, start, end, dst)
                ok += 1
            except Exception as exc:
                errors.append(f"{d.file}: {exc}")

        if errors:
            self._app_state.errorOccurred.emit(f"Exported {ok}/{len(rows)} snippets. First error: {errors[0]}")
        else:
            self._app_state.statusMessage.emit(f"Exported {ok} snippet(s) to {folder.name}")

    # helpers

    def _set_controls_enabled(self, enabled: bool) -> None:
        for w in (
            self.ui.campaign_combo,
            self.ui.max_per_spin,
            self.ui.padding_button,
            self.ui.columns_button,
            self.ui.export_button,
        ):
            w.setEnabled(enabled)

    def _set_padding_widgets(self, before: float, after: float) -> None:
        """Push values into the spinboxes without triggering update_padding back."""
        for spin, value in (
            (self.pad_before_spin, before),
            (self.pad_after_spin, after),
        ):
            spin.blockSignals(True)
            try:
                spin.setValue(value)
            finally:
                spin.blockSignals(False)

    def _visible_column_names(self) -> list[str]:
        hidden = set(self.ui.detections_table.hiddenColumnNames())
        return [
            name
            for name, idx in sorted(COLUMNS_BY_NAME.items(), key=lambda kv: kv[1])
            if not name.startswith("_") and name not in hidden
        ]


def _write_visible_csv(path: Path, detections: list[Detection], columns: list[str]) -> None:
    """Write *detections* with *columns* as headers, in column order."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for d in detections:
            writer.writerow(
                ("" if (v := COLUMN_GETTERS[c](d)) is None else v) for c in columns
            )


def _snippet_filename(d: Detection, start: float, end: float) -> str:
    """Build a descriptive .wav filename per detection.

    Slimmer than the original AG Grid version: campaign / aru / scientific
    name / start / end / confidence is enough for human-readable export.
    """
    try:
        stamp = datetime.fromisoformat(d.recording_time).strftime("%Y%m%d_%H%M%S")
    except (ValueError, TypeError):
        stamp = d.recording_time or "unknown_time"
    parts = [
        d.campaign or "unknown",
        d.aru or "unknown",
        (d.scientific_name or d.species or "unknown").replace(" ", "_"),
        stamp,
        f"{start:.1f}-{end:.1f}",
        f"conf{d.confidence:.4f}",
    ]
    return "_-_".join(p for p in parts if p) + ".wav"
