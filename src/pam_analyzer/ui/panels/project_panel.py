"""Project settings panel: shows the project folder and edits the
SD-card regex and preferred species language."""

import re
from dataclasses import replace

from PySide6.QtWidgets import QWidget

from ...domain import Project
from ...infrastructure import paths
from ..app_state import AppState
from .ui_project_panel import Ui_ProjectPanel

# Static fallback locale list. Real list comes from the BirdNET adapter in Phase 6+.
_DEFAULT_LOCALES: tuple[str, ...] = (
    "en",
    "de",
    "es",
    "fr",
    "it",
    "nl",
    "pl",
    "pt",
    "ru",
    "sv",
)

_VALID_STYLE = "color: #2e7d32;"
_INVALID_STYLE = "color: #c62828;"


class ProjectPanel(QWidget):
    def __init__(self, app_state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ui = Ui_ProjectPanel()
        self.ui.setupUi(self)

        self._app_state = app_state
        # Suppress edit handlers while we populate fields from a loaded project.
        self._loading = False

        self._populate_locale_combo()
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

    def _populate_locale_combo(self) -> None:
        self.ui.species_lang_combo.addItems(_DEFAULT_LOCALES)

    # rendering

    def _render(self, project: Project | None) -> None:
        self._loading = True
        try:
            enabled = project is not None
            for w in (
                self.ui.sdcard_pattern_edit,
                self.ui.species_lang_combo,
            ):
                w.setEnabled(enabled)

            if project is None:
                self.ui.folder_label.clear()
                self.ui.sdcard_pattern_edit.clear()
                self.ui.species_lang_combo.setCurrentText("")
                self._refresh_regex_indicator("")
                return

            self.ui.folder_label.setText(paths.contract_user_path(str(project.folder)))
            self.ui.sdcard_pattern_edit.setText(project.sdcard_name_pattern)
            self._set_combo_value(project.preferred_species_lang)

            self._refresh_regex_indicator(project.sdcard_name_pattern)
        finally:
            self._loading = False

    def _set_combo_value(self, lang: str) -> None:
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
