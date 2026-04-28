"""Project settings panel: edit a loaded project's audio root, output dir,
SD-card regex, and preferred species language."""

import re
from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QLineEdit, QWidget

from ...domain import Project
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

        self.ui.audio_browse_button.clicked.connect(lambda: self._browse_directory(self.ui.audio_path_edit))
        self.ui.output_browse_button.clicked.connect(lambda: self._browse_directory(self.ui.output_path_edit))

        self.ui.audio_path_edit.editingFinished.connect(self._on_audio_changed)
        self.ui.output_path_edit.editingFinished.connect(self._on_output_changed)
        self.ui.sdcard_pattern_edit.textChanged.connect(self._on_sdcard_pattern_changed)
        self.ui.species_lang_combo.editTextChanged.connect(self._on_species_lang_changed)

    def _populate_locale_combo(self) -> None:
        self.ui.species_lang_combo.addItems(_DEFAULT_LOCALES)

    # rendering

    def _render(self, project: Project | None) -> None:
        self._loading = True
        try:
            enabled = project is not None
            for w in (
                self.ui.audio_path_edit,
                self.ui.audio_browse_button,
                self.ui.output_path_edit,
                self.ui.output_browse_button,
                self.ui.sdcard_pattern_edit,
                self.ui.species_lang_combo,
            ):
                w.setEnabled(enabled)

            if project is None:
                self.ui.audio_path_edit.clear()
                self.ui.output_path_edit.clear()
                self.ui.sdcard_pattern_edit.clear()
                self.ui.species_lang_combo.setCurrentText("")
                self.ui.audio_warning_label.setVisible(False)
                self._refresh_regex_indicator("")
                self._update_output_placeholder(None)
                return

            self.ui.audio_path_edit.setText(str(project.audio_recordings_path))
            self.ui.output_path_edit.setText(
                "" if project.detections_output_path is None
                else str(project.detections_output_path)
            )
            self.ui.sdcard_pattern_edit.setText(project.sdcard_name_pattern)
            self._set_combo_value(project.preferred_species_lang)

            self._refresh_audio_warning(str(project.audio_recordings_path))
            self._refresh_regex_indicator(project.sdcard_name_pattern)
            self._update_output_placeholder(project)
        finally:
            self._loading = False

    def _set_combo_value(self, lang: str) -> None:
        idx = self.ui.species_lang_combo.findText(lang)
        if idx >= 0:
            self.ui.species_lang_combo.setCurrentIndex(idx)
        else:
            self.ui.species_lang_combo.setEditText(lang)

    # handlers

    def _on_audio_changed(self) -> None:
        if self._loading:
            return
        text = self.ui.audio_path_edit.text().strip()
        self._refresh_audio_warning(text)
        new_path = Path(text).expanduser() if text else Path.home()
        self._apply(audio_recordings_path=new_path)
        # Recompute output placeholder against the new audio root.
        self._update_output_placeholder(self._app_state.project)

    def _on_output_changed(self) -> None:
        if self._loading:
            return
        text = self.ui.output_path_edit.text().strip()
        self._apply(detections_output_path=(Path(text).expanduser() if text else None))

    def _on_sdcard_pattern_changed(self, value: str) -> None:
        self._refresh_regex_indicator(value)
        if self._loading:
            return
        self._apply(sdcard_name_pattern=value)

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

    def _browse_directory(self, target: QLineEdit) -> None:
        start = target.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose directory", start)
        if not chosen:
            return
        target.setText(chosen)
        # editingFinished does not fire when we set the text programmatically;
        # invoke the same handler explicitly.
        if target is self.ui.audio_path_edit:
            self._on_audio_changed()
        else:
            self._on_output_changed()

    def _refresh_audio_warning(self, text: str) -> None:
        path = Path(text).expanduser() if text else None
        self.ui.audio_warning_label.setVisible(bool(text) and (path is None or not path.exists()))

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

    def _update_output_placeholder(self, project: Project | None) -> None:
        if project is None:
            self.ui.output_path_edit.setPlaceholderText("(defaults to {audio root}/{project}-detections)")
            return
        default = project.audio_recordings_path / f"{project.name}-detections"
        self.ui.output_path_edit.setPlaceholderText(f"(default: {default})")
