import re
from dataclasses import dataclass
from dataclasses import fields as dc_fields
from pathlib import Path

from nicegui import app, ui
from webview import FileDialog

from pam_analyzer.core.project_settings import ProjectSettings
from pam_analyzer.core.utils import get_project_name

# Session state, reset on each app launch, not persisted.
_project_loaded: bool = False
_project_path: Path | None = None
_project_dirty: bool = False

# Project settings loaded from TOML on project open, reset to defaults on new.
project_settings: ProjectSettings = ProjectSettings()


@dataclass
class ProjectStatus:
    """Whether the mandatory settings are valid enough for the app to be usable.
    Exists as a dataclass so NiceGUI can bind UI elements (e.g. tabs) to its fields."""

    ready: bool = False


project_status = ProjectStatus()


def _update_project_status() -> None:
    """Recompute project status. Call whenever project_settings change."""
    try:
        re.compile(project_settings.sdcard_name_pattern)
        valid_pattern = bool(project_settings.sdcard_name_pattern)
    except re.error:
        valid_pattern = False
    project_status.ready = bool(project_settings.audio_recordings_path) and Path(project_settings.audio_recordings_path).exists() and valid_pattern


def is_project_loaded() -> bool:
    """Return True if a project has been loaded in this session."""
    return _project_loaded


def get_project_path() -> Path | None:
    """Return the path of the currently open project file, or None if unsaved."""
    return _project_path


def get_project_output_base() -> Path:
    """Return the detections output directory for the current project.

    Uses the detections_output_path if set, otherwise falls back to
    ``{audio_recordings_path}/{project_name}-detections``.
    """
    if project_settings.detections_output_path:
        return Path(project_settings.detections_output_path)
    return Path(project_settings.audio_recordings_path) / f'{get_project_name(get_project_path()) or "project"}-detections'


# Internal helpers
def _apply_settings(settings: ProjectSettings) -> None:
    """Replace the live project settings with *settings*."""
    for f in dc_fields(ProjectSettings):
        setattr(project_settings, f.name, getattr(settings, f.name))


def _update_title() -> None:
    """Sync the native window title with the current project state."""
    if not _project_loaded:
        app.native.main_window.set_title('PAM Analyzer')
    elif not _project_path:
        app.native.main_window.set_title('PAM Analyzer - *Unsaved Project*')
    else:
        suffix = ' *' if _project_dirty else ''
        app.native.main_window.set_title(f'PAM Analyzer - {_project_path.name}{suffix}')


def _on_setting_change(attr: str, value: str) -> None:
    """Update *attr* on the live project settings and mark the project as having unsaved changes."""
    global _project_dirty
    setattr(project_settings, attr, value)
    _project_dirty = True
    _update_project_status()
    _update_title()


async def _pick_directory(attr: str, input_ref) -> None:
    result = await app.native.main_window.create_file_dialog(FileDialog.FOLDER)
    if result:
        value = result[0]
        input_ref.value = value
        _on_setting_change(attr, value)


def build_settings_inputs() -> None:
    """Render project settings inputs with browse buttons and live validation."""

    # Audio recordings path
    def _on_audio_change(e) -> None:
        _on_setting_change('audio_recordings_path', e.value)
        audio_warning.set_visibility(bool(e.value) and not Path(e.value).exists())
        if not project_settings.detections_output_path:
            placeholder = (Path(e.value) / f'{get_project_name(get_project_path()) or "project"}-detections').as_posix()
            output_input.props(f'placeholder="{placeholder}"')

    with ui.column().classes('w-full gap-0'):
        with ui.row().classes('w-full items-center gap-2'):
            audio_input = ui.input(
                label='Audio recordings root path',
                value=project_settings.audio_recordings_path,
                on_change=_on_audio_change,
            ).classes('flex-1')
            ui.button(
                icon='folder_open',
                on_click=lambda: _pick_directory('audio_recordings_path', audio_input),
            ).props('flat dense')
        audio_warning = ui.label('⚠ Path does not exist').classes('text-warning text-caption')
        ui.label('Import destination, or an existing directory of WAV subdirectories. The Import panel can be skipped if files are already in place').classes('text-caption text-grey')
        initial_audio = project_settings.audio_recordings_path
        audio_warning.set_visibility(bool(initial_audio) and not Path(initial_audio).exists())

    # Detections output path
    with ui.column().classes('w-full gap-0'):
        _proj_path = get_project_path()
        _proj_name = _proj_path.stem if _proj_path else 'project'
        placeholder = (Path(project_settings.audio_recordings_path) / f'{_proj_name}-detections').as_posix() if project_settings.audio_recordings_path else ''
        with ui.row().classes('w-full items-center gap-2'):
            output_input = ui.input(
                label='Detections output path',
                value=project_settings.detections_output_path,
                placeholder=placeholder,
                on_change=lambda e: _on_setting_change('detections_output_path', e.value),
            ).classes('flex-1').props('stack-label')
            ui.button(
                icon='folder_open',
                on_click=lambda: _pick_directory('detections_output_path', output_input),
            ).props('flat dense')
        ui.label('Where detection CSVs are written').classes('text-caption text-grey')

    # SD card name pattern
    def _refresh_regex_indicator(value: str) -> None:
        try:
            re.compile(value)
            regex_indicator.set_text('● valid')
            regex_indicator.classes(remove='text-negative', add='text-positive')
        except re.error:
            regex_indicator.set_text('✕ invalid')
            regex_indicator.classes(remove='text-positive', add='text-negative')

    def _on_sdcard_regex_change(e) -> None:
        _on_setting_change('sdcard_name_pattern', e.value)
        _refresh_regex_indicator(e.value)

    with ui.column().classes('w-full gap-0'):
        with ui.row().classes('w-full items-center gap-2'):
            ui.input(
                label='SD card name pattern (regex)',
                value=project_settings.sdcard_name_pattern,
                on_change=_on_sdcard_regex_change,
            ).classes('flex-1')
            regex_indicator = ui.label('● valid').classes('text-positive text-caption')
        ui.label('Regex matched against SD card volume names, e.g. ^MSD-').classes('text-caption text-grey')

    # Preferred species language
    def _get_lang_options() -> list[str]:
        try:
            from pam_analyzer.core.birdnet_runner import get_available_locales
            return get_available_locales()
        except Exception:
            return []

    with ui.column().classes('w-full gap-0'):
        # "en" uses BirdNET's native US English (no label file needed/available)
        lang_options = sorted({'en', *_get_lang_options()})
        current_lang = project_settings.preferred_species_lang
        if current_lang not in lang_options:
            current_lang = 'en'
        ui.select(
            options=lang_options,
            label='Preferred species language',
            value=current_lang,
            on_change=lambda e: _on_setting_change('preferred_species_lang', e.value),
        ).classes('w-52').props('outlined dense options-dense')
        ui.label('Language used for the Species column in all outputs').classes('text-caption text-grey')

    # Set initial indicator state
    _refresh_regex_indicator(project_settings.sdcard_name_pattern)


# Project lifecycle
def load_project(path: Path) -> None:
    """Load the project TOML at *path*, apply its settings, and mark the session as loaded."""
    global _project_loaded, _project_path, _project_dirty
    _apply_settings(ProjectSettings.load(path))
    _project_path = path
    _project_loaded = True
    _project_dirty = False
    _update_project_status()
    _update_title()


def new_project() -> None:
    """Create a new unsaved in-memory project and enter the main UI."""
    global _project_loaded
    reset_project()
    _project_loaded = True
    _update_title()


def reset_project() -> None:
    """Reset all project state to defaults and return to the unloaded state."""
    global _project_loaded, _project_path, _project_dirty
    _apply_settings(ProjectSettings())
    _project_path = None
    _project_loaded = False
    _project_dirty = False
    _update_project_status()
    _update_title()


def save_project_as(path: Path) -> None:
    """Save the current project settings to *path* and clear the dirty flag."""
    global _project_path, _project_dirty
    project_settings.save(path)
    _project_path = path
    _project_dirty = False
    _update_title()



class ProjectPanel:
    def build(self) -> None:
        """Render the Project panel: editable project settings."""
        ui.label('Project Settings').classes('text-h5')
        build_settings_inputs()
