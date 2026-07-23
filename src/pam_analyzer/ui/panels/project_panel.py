"""Project settings panel: shows the project folder and edits the
SD-card regex and preferred species language."""

import re
from dataclasses import replace

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QCheckBox, QWidget

from ...domain import MAX_OVERLAP_S, Project
from ...infrastructure import paths
from ...infrastructure.birdnet_lib import normalize_lang_code
from ..app_state import AppState
from .ui_project_panel import Ui_ProjectPanel

# Columns in the extra-languages checkbox grid. The codes are short, so a few
# columns fit the panel width while keeping the grid only a handful of rows tall.
_LOCALE_GRID_COLUMNS = 6

_VALID_STYLE = "color: #2e7d32;"
_INVALID_STYLE = "color: #c62828;"


class ProjectPanel(QWidget):
    def __init__(
        self,
        app_state: AppState,
        available_locales: tuple[str, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ui = Ui_ProjectPanel()
        self.ui.setupUi(self)

        self._app_state = app_state
        self._available_locales = available_locales
        self._locale_checks: dict[str, QCheckBox] = {}
        # Suppress edit handlers while we populate fields from a loaded project.
        self._loading = False

        # Single source of truth for the overlap cap: size the slider from it.
        self.ui.overlap_slider.setMaximum(int(round(MAX_OVERLAP_S * 10)))
        self._populate_locale_combo()
        self._build_locales_grid()
        self._wire_signals()
        self._render(app_state.project)

    # wiring

    def _wire_signals(self) -> None:
        self._app_state.projectChanged.connect(self._render)

        # Validate live so the indicator reacts as the user types, but only
        # commit the pattern when editing finishes. Committing on every
        # keystroke would broadcast projectChanged (and the analysis/import/
        # inventory signals) to every other panel, whose mid-keystroke
        # re-render steals focus from this field.
        self.ui.sdcard_pattern_edit.textChanged.connect(self._refresh_regex_indicator)
        self.ui.sdcard_pattern_edit.editingFinished.connect(self._on_sdcard_pattern_changed)
        self.ui.species_lang_combo.editTextChanged.connect(self._on_species_lang_changed)
        self.ui.folder_label.linkActivated.connect(self._open_folder)
        # Sliders fire continuously while dragged, so these save silently
        # (no projectChanged broadcast) to avoid re-rendering every panel
        # mid-drag. See AppState.save_project_fields.
        self.ui.min_conf_slider.valueChanged.connect(self._on_min_conf_changed)
        self.ui.overlap_slider.valueChanged.connect(self._on_overlap_changed)

    def _populate_locale_combo(self) -> None:
        # Same source as the extra-languages grid, so both controls offer the
        # model's real locale codes (e.g. en_us/en_uk, not a bare en).
        self.ui.species_lang_combo.addItems(sorted(self._available_locales))

    def _build_locales_grid(self) -> None:
        """Lay the model's locale codes out as a checkbox grid.

        Built once: the available set is fixed for the app's lifetime (both
        runners expose the same list), so unlike a per-run control it never
        needs rebuilding.
        """
        self._locale_checks = {}
        grid = self.ui.locales_grid
        for i, loc in enumerate(sorted(self._available_locales)):
            chk = QCheckBox(loc, self)
            chk.toggled.connect(self._on_locales_changed)
            self._locale_checks[loc] = chk
            grid.addWidget(chk, i // _LOCALE_GRID_COLUMNS, i % _LOCALE_GRID_COLUMNS)
        # A stretch column past the last code packs the checkboxes to the left.
        grid.setColumnStretch(_LOCALE_GRID_COLUMNS, 1)

    # rendering

    def _render(self, project: Project | None) -> None:
        self._loading = True
        try:
            enabled = project is not None
            for w in (
                self.ui.sdcard_pattern_edit,
                self.ui.species_lang_combo,
                self.ui.min_conf_slider,
                self.ui.overlap_slider,
            ):
                w.setEnabled(enabled)
            for chk in self._locale_checks.values():
                chk.setEnabled(enabled)

            if project is None:
                self.ui.folder_label.clear()
                self.ui.sdcard_pattern_edit.clear()
                self.ui.species_lang_combo.setCurrentText("")
                self._refresh_regex_indicator("")
                self._set_slider_values(min_conf=0.25, overlap=0.0)
                self._set_locale_checks(())
                return

            folder_text = paths.contract_user_path(str(project.folder))
            self.ui.folder_label.setText(f'<a href="open">{folder_text}</a>')
            self.ui.sdcard_pattern_edit.setText(project.sdcard_name_pattern)
            self._set_combo_value(project.preferred_species_lang)
            self._set_slider_values(min_conf=project.min_conf, overlap=project.overlap)
            self._set_locale_checks(project.locales)

            self._refresh_regex_indicator(project.sdcard_name_pattern)
        finally:
            self._loading = False

    def _set_slider_values(self, *, min_conf: float, overlap: float) -> None:
        for slider, value in (
            (self.ui.min_conf_slider, int(round(min_conf * 100))),
            (self.ui.overlap_slider, int(round(overlap * 10))),
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

    def _set_combo_value(self, lang: str) -> None:
        # A legacy short code (e.g. "en") is not one of the model's codes, so
        # normalize it to its canonical form ("en_us") before selecting, the
        # same mapping the runner applies at analysis time. A code we still
        # don't recognise is shown as-is rather than silently dropped.
        lang = normalize_lang_code(lang)
        idx = self.ui.species_lang_combo.findText(lang)
        if idx >= 0:
            self.ui.species_lang_combo.setCurrentIndex(idx)
        else:
            self.ui.species_lang_combo.setEditText(lang)

    # handlers

    def _on_sdcard_pattern_changed(self) -> None:
        if self._loading:
            return
        self._apply(sdcard_name_pattern=self.ui.sdcard_pattern_edit.text())

    def _on_species_lang_changed(self, value: str) -> None:
        if self._loading:
            return
        self._apply(preferred_species_lang=value.strip() or "en")

    def _on_min_conf_changed(self, value: int) -> None:
        self._refresh_slider_labels()
        if self._loading:
            return
        self._app_state.save_project_fields(min_conf=value / 100.0)

    def _on_overlap_changed(self, value: int) -> None:
        self._refresh_slider_labels()
        if self._loading:
            return
        self._app_state.save_project_fields(overlap=value / 10.0)

    def _on_locales_changed(self, _checked: bool) -> None:
        if self._loading:
            return
        locales = tuple(loc for loc, chk in self._locale_checks.items() if chk.isChecked())
        self._app_state.save_project_fields(locales=locales)

    def _open_folder(self, _link: str) -> None:
        project = self._app_state.project
        if project is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(project.folder)))

    # side-effects

    def _apply(self, **changes: object) -> None:
        """Replace the project in AppState with the modified copy."""
        project = self._app_state.project
        if project is None:
            return
        try:
            new_project = replace(project, **changes)
        except TypeError:
            return
        self._app_state.update_project(new_project)

    def _refresh_regex_indicator(self, value: str) -> None:
        if not value:
            self.ui.regex_indicator_label.setText("✕ empty")
            self.ui.regex_indicator_label.setStyleSheet(_INVALID_STYLE)
            return
        try:
            re.compile(value)
        except re.error:
            self.ui.regex_indicator_label.setText("✕ invalid")
            self.ui.regex_indicator_label.setStyleSheet(_INVALID_STYLE)
        else:
            self.ui.regex_indicator_label.setText("● valid")
            self.ui.regex_indicator_label.setStyleSheet(_VALID_STYLE)
