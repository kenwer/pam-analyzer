"""Campaign detail / edit form."""

import shutil
from collections.abc import Callable
from pathlib import Path

from nicegui import app, run, ui
from webview import FileDialog

from pam_analyzer.core.campaign_settings import (
    SPECIES_LIST_FILENAME,
    CampaignSettings,
)
from pam_analyzer.core.utils import contract_user_path, open_file, valid_lat, valid_lon
from pam_analyzer.panels.project import project_settings


class CampaignDetail:
    def __init__(
        self,
        on_saved: Callable[[str], None],
        on_deleted: Callable[[], None],
        on_cancelled: Callable[[], None],
    ) -> None:
        self._on_saved = on_saved
        self._on_deleted = on_deleted
        self._on_cancelled = on_cancelled

        # State
        self._selected_name: str | None = None
        self._campaign_paths: dict[str, Path] = {}
        self._location_marker = None
        self._species_file_path: Path | None = None  # path to the saved file in the campaign folder

        # Original values for change detection (edit mode only)
        self._original_name: str = ''
        self._original_mode: str = 'location'
        self._original_lat: str = ''
        self._original_lon: str = ''
        self._original_species_content: str = ''

        # UI refs
        self._empty_hint: ui.column | None = None
        self._confirm_row: ui.row | None = None
        self._form_container: ui.column | None = None
        self._name_input: ui.input | None = None
        self._filter_radio: ui.radio | None = None
        self._location_section: ui.column | None = None
        self._species_section: ui.column | None = None
        self._location_map = None
        self._lat_input: ui.input | None = None
        self._lon_input: ui.input | None = None
        self._species_label: ui.label | None = None
        self._species_textarea: ui.textarea | None = None
        self._save_btn: ui.button | None = None

    def build(self) -> None:
        ui.add_css("""
            .fill-height-textarea, .fill-height-textarea .q-field,
            .fill-height-textarea .q-field__inner, .fill-height-textarea .q-field__control { height: 100%; }
            .fill-height-textarea textarea { height: 100% !important; resize: none; }
        """)
        with ui.column().classes('flex-1 gap-2 items-center justify-center w-full text-grey') as self._empty_hint:
            ui.icon('folder_special', size='48px').classes('text-grey-4')
            ui.label('Select a campaign to view or edit').classes('text-subtitle2 text-grey')
            ui.label('or use + to create a new one').classes('text-caption text-grey-5')

        with ui.row().classes('items-center gap-2') as self._confirm_row:
            ui.label('Delete this campaign?').classes('text-caption text-negative')
            ui.button('Cancel', on_click=self._on_cancel_delete).props('flat dense')
            ui.button('Delete', icon='delete', on_click=self._save_delete).props('flat dense color=negative')
        self._confirm_row.set_visibility(False)

        with ui.column().classes('w-full flex-1 gap-3 p-4 border border-dashed rounded') as self._form_container:
            self._name_input = ui.input(
                'Campaign folder name',
                placeholder='e.g. 20260114-20260216-Federsee',
                on_change=lambda _: self._validate_form(),
            ).classes('w-full')
            with ui.row().classes('items-center gap-4'):
                ui.label('Filter').classes('text-caption text-grey')
                self._filter_radio = ui.radio(
                    {'location': 'Location', 'list': 'Species list'},
                    value='location',
                    on_change=self._on_filter_mode_change,
                ).props('inline')
            with ui.column().classes('gap-2 w-full flex-1 min-h-0') as self._location_section:
                with ui.element('div').classes('relative w-full rounded flex-1').style('min-height: 150px'):
                    self._location_map = ui.leaflet(center=(20, 0), zoom=2, options={'attributionControl': False}).classes('w-full h-full rounded')
                    ui.label('© OpenStreetMap').classes('absolute text-grey-8').style('top: 4px; right: 4px; z-index: 1000; pointer-events: none; font-size: 10px')
                self._location_map.on('map-click', self._on_map_click)
                with ui.row().classes('items-center gap-2'):
                    self._lat_input = ui.input(
                        'Latitude',
                        placeholder='-90 … 90',
                        on_change=lambda _: self._on_latlon_input_change(),
                    ).classes('w-36')
                    self._lon_input = ui.input(
                        'Longitude',
                        placeholder='-180 … 180',
                        on_change=lambda _: self._on_latlon_input_change(),
                    ).classes('w-36')
                    ui.label('Click the map to set location').classes('text-caption text-grey')
            with ui.column().classes('gap-1 w-full flex-1 min-h-0') as self._species_section:
                with ui.row().classes('items-center gap-2'):
                    ui.button(icon='folder_open', on_click=self._pick_species_list).props('flat dense').tooltip('Import from file')
                    self._species_label = ui.label('').classes('text-caption text-grey')
                    self._species_label.on('click', lambda: open_file(str(self._species_file_path)) if self._species_file_path else None)
                self._species_textarea = (
                    ui.textarea(
                        placeholder='One species per line…',
                        on_change=lambda _: self._validate_form(),
                    )
                    .classes('w-full flex-1 fill-height-textarea')
                    .props('outlined')
                )
            self._species_section.set_visibility(False)
            with ui.row().classes('justify-end gap-2 w-full'):
                ui.button('Cancel', on_click=self._on_cancel).props('flat')
                self._save_btn = ui.button('Create', on_click=self._save).props('color=primary')
        self._form_container.set_visibility(False)

    def show_empty(self) -> None:
        self._form_container.set_visibility(False)
        self._confirm_row.set_visibility(False)
        self._empty_hint.set_visibility(True)

    def open_new(self, campaign_paths: dict[str, Path]) -> None:
        self._selected_name = None
        self._campaign_paths = campaign_paths
        self._empty_hint.set_visibility(False)
        self._confirm_row.set_visibility(False)
        self._name_input.value = ''
        self._filter_radio.value = 'location'
        self._location_section.set_visibility(True)
        self._species_section.set_visibility(False)
        self._lat_input.value = ''
        self._lon_input.value = ''
        if self._location_marker is not None:
            self._location_map.remove_layer(self._location_marker)
            self._location_marker = None
        self._location_map.set_center((20, 0))
        self._location_map.set_zoom(2)
        self._set_species_label('Import an existing species file or create one from scratch below')
        self._species_textarea.value = ''
        self._save_btn.set_text('Create')
        self._validate_form()
        self._form_container.set_visibility(True)

    def open_edit(self, name: str, campaign_dir: Path, campaign_paths: dict[str, Path]) -> None:
        self._selected_name = name
        self._campaign_paths = campaign_paths
        self._empty_hint.set_visibility(False)
        self._confirm_row.set_visibility(False)
        settings = CampaignSettings.load(campaign_dir)
        self._name_input.value = name
        self._filter_radio.value = settings.species_filter_mode
        self._location_section.set_visibility(settings.species_filter_mode == 'location')
        self._species_section.set_visibility(settings.species_filter_mode == 'list')
        if settings.species_filter_mode == 'location':
            self._lat_input.value = str(settings.latitude)
            self._lon_input.value = str(settings.longitude)
            self._set_marker(settings.latitude, settings.longitude)
            self._location_map.set_center((settings.latitude, settings.longitude))
            self._location_map.set_zoom(6)
        else:
            if self._location_marker is not None:
                self._location_map.remove_layer(self._location_marker)
                self._location_marker = None
            species_file = campaign_dir / SPECIES_LIST_FILENAME
            if species_file.exists():
                self._species_textarea.value = species_file.read_text()
                self._set_species_label(str(Path(name) / SPECIES_LIST_FILENAME), file_path=species_file)
            else:
                self._species_textarea.value = ''
                self._set_species_label('Import an existing species file or create one from scratch below')
        self._original_name = name
        self._original_mode = settings.species_filter_mode
        self._original_lat = str(settings.latitude) if settings.species_filter_mode == 'location' else ''
        self._original_lon = str(settings.longitude) if settings.species_filter_mode == 'location' else ''
        self._original_species_content = self._species_textarea.value if settings.species_filter_mode == 'list' else ''
        self._save_btn.set_text('Save')
        self._validate_form()
        self._form_container.set_visibility(True)

    def show_delete_confirm(self, name: str, campaign_paths: dict[str, Path]) -> None:
        self._selected_name = name
        self._campaign_paths = campaign_paths
        self._empty_hint.set_visibility(False)
        self._form_container.set_visibility(False)
        self._confirm_row.set_visibility(True)

    def _on_cancel_delete(self) -> None:
        self._confirm_row.set_visibility(False)
        if self._selected_name and self._selected_name in self._campaign_paths:
            self.open_edit(self._selected_name, self._campaign_paths[self._selected_name], self._campaign_paths)
        else:
            self._empty_hint.set_visibility(True)

    def _on_cancel(self) -> None:
        self._form_container.set_visibility(False)
        self._selected_name = None
        self._on_cancelled()

    def _has_changes(self) -> bool:
        if self._selected_name is None:
            return True
        if self._name_input.value.strip() != self._original_name:
            return True
        if self._filter_radio.value != self._original_mode:
            return True
        if self._filter_radio.value == 'location':
            return self._lat_input.value != self._original_lat or self._lon_input.value != self._original_lon
        return self._species_textarea.value != self._original_species_content

    def _validate_form(self) -> None:
        new_name = self._name_input.value.strip()
        name_ok = bool(new_name) and '/' not in new_name and '\\' not in new_name and (new_name == self._selected_name or new_name not in self._campaign_paths)
        if self._filter_radio.value == 'location':
            ok = name_ok and valid_lat(self._lat_input.value) and valid_lon(self._lon_input.value)
        else:
            ok = name_ok and bool(self._species_textarea.value.strip())
        self._save_btn.set_enabled(ok and self._has_changes())

    def _set_species_label(self, text: str, file_path: Path | None = None) -> None:
        self._species_file_path = file_path
        self._species_label.set_text(text)
        if file_path:
            self._species_label.classes(remove='text-grey', add='text-blue-500 cursor-pointer')
        else:
            self._species_label.classes(remove='text-blue-500 cursor-pointer', add='text-grey')

    def _set_marker(self, lat: float, lon: float) -> None:
        if self._location_marker is None:
            self._location_marker = self._location_map.marker(latlng=(lat, lon))
        else:
            self._location_marker.move(lat, lon)

    def _on_filter_mode_change(self, e) -> None:
        self._location_section.set_visibility(e.value == 'location')
        self._species_section.set_visibility(e.value == 'list')
        if e.value == 'list' and self._original_mode != 'list':
            self._species_textarea.value = ''
            self._set_species_label('Import an existing species file or create one from scratch below')
            self._original_species_content = ''
        self._validate_form()

    def _on_map_click(self, e) -> None:
        lat = round(e.args['latlng']['lat'], 2)
        lon = round(e.args['latlng']['lng'], 2)
        self._lat_input.value = str(lat)
        self._lon_input.value = str(lon)
        self._set_marker(lat, lon)
        self._validate_form()

    def _on_latlon_input_change(self) -> None:
        self._validate_form()
        if valid_lat(self._lat_input.value) and valid_lon(self._lon_input.value):
            lat, lon = float(self._lat_input.value), float(self._lon_input.value)
            self._set_marker(lat, lon)
            self._location_map.set_center((lat, lon))
            self._location_map.set_zoom(10)

    async def _pick_species_list(self) -> None:
        result = await app.native.main_window.create_file_dialog(
            FileDialog.OPEN,
            allow_multiple=False,
            file_types=['Text files (*.txt)', 'All files (*.*)'],
        )
        if result:
            src = Path(result[0])
            self._species_textarea.value = await run.io_bound(src.read_text)
            self._set_species_label(f'Imported from {contract_user_path(str(src))}')
            self._validate_form()

    async def _save(self) -> None:
        new_name = self._name_input.value.strip()
        creating = self._selected_name is None

        if creating:
            campaign_dir = Path(project_settings.audio_recordings_path) / new_name
            await run.io_bound(lambda: campaign_dir.mkdir(parents=True, exist_ok=True))
            action = 'created'
        else:
            old_dir = self._campaign_paths[self._selected_name]
            if new_name != self._selected_name:
                new_dir = old_dir.parent / new_name
                await run.io_bound(old_dir.rename, new_dir)
                campaign_dir = new_dir
                action = f'renamed to "{new_name}"'
            else:
                campaign_dir = old_dir
                action = 'saved'

        settings = CampaignSettings(species_filter_mode=self._filter_radio.value)
        if self._filter_radio.value == 'location':
            settings.latitude = float(self._lat_input.value)
            settings.longitude = float(self._lon_input.value)
        else:
            species_file = campaign_dir / SPECIES_LIST_FILENAME
            await run.io_bound(species_file.write_text, self._species_textarea.value)
        await run.io_bound(settings.save, campaign_dir)

        ui.notify(f'Campaign "{new_name}" {action}', type='positive')
        self._on_saved(new_name)

    async def _save_delete(self) -> None:
        if self._selected_name is None:
            return
        name = self._selected_name
        await run.io_bound(shutil.rmtree, self._campaign_paths[name])
        self._form_container.set_visibility(False)
        self._confirm_row.set_visibility(False)
        ui.notify(f'Campaign "{name}" deleted', type='warning')
        self._on_deleted()
