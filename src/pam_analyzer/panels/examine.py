import csv
import io
import json
import sys
from pathlib import Path

from nicegui import app, run, ui
from webview import FileDialog

from pam_analyzer.core.birdnet_runner import build_locale_reverse_map
from pam_analyzer.core.detections_io import (
    audio_duration,
    build_snippet_filename,
    extract_audio_snippet,
)
from pam_analyzer.core.examine_data_manager import ALL_CAMPAIGNS, ExamineDataManager
from pam_analyzer.core.utils import get_project_name, resolve_audio_path
from pam_analyzer.panels.detections_grid import DetectionsGrid
from pam_analyzer.panels.project import (
    _on_setting_change,
    get_project_output_base,
    get_project_path,
    project_settings,
)

_MAX_ROWS = 500_000  # AG Grid limit; optimized via chunked transfer and minimal data payload


class ExaminePanel:
    def __init__(self) -> None:
        self._data = ExamineDataManager()
        self._grid = DetectionsGrid()
        self._media_route_added = False

        # UI refs
        self._campaign_select: ui.select | None = None
        self._status_label: ui.label | None = None
        self._progress_bar: ui.linear_progress | None = None
        self._cols_menu_container: ui.column | None = None
        self._action_buttons: list = []  # [padding_btn, cols_btn, export_btn]

        # Max detections per ARU/species filter
        self._max_detections_per_aru_and_species: int = 0

    def build(self) -> None:
        ui.label('Examine Data').classes('text-h5')

        # Filter bar: campaign, max detections, hint
        with ui.row().classes('items-center gap-4 mb-2 w-full'):
            self._campaign_select = (
                ui.select(
                    options={},
                    label='Campaign',
                    on_change=lambda e: self._on_campaign_change(e.value),
                )
                .classes('w-80')
                .props('outlined dense options-dense')
            )
            (
                ui.select(
                    options=['All', '1', '2', '3', '4', '5', '10', '15', '20', '25', '30', '50', '100'],
                    value='All',
                    label='Max detections per ARU/Species',
                    on_change=self._on_max_detections_per_aru_and_species_change,
                )
                .classes('w-60')
                .props('outlined dense options-dense')
            )
            ui.space()
            ui.label('Hint: Shift+click column headers to sort by multiple columns').classes('text-caption text-grey self-center')

        ui.separator().classes('my-2')

        # Table bar: status label, progress bar, padding/columns/export actions
        with ui.row().classes('items-center w-full mb-1'):
            self._status_label = ui.label('Loading...').classes('text-caption text-grey self-center')
            self._progress_bar = ui.linear_progress(value=0, show_value=False).classes('flex-1 mx-4')
            ui.space()
            _padding_btn = ui.button(icon='tune').props('flat dense round')
            _padding_btn.disable()
            with _padding_btn:
                ui.tooltip('Playback padding')
                with ui.menu():
                    with ui.column().classes('p-4 gap-3'):
                        ui.label('Playback Padding').classes('text-subtitle2 font-bold')
                        ui.number(
                            label='Before (s)',
                            value=project_settings.snippet_padding_before,
                            min=0, step=0.5, format='%.1f',
                            on_change=self._on_padding_before_change,
                        ).classes('w-36').props('outlined dense')
                        ui.number(
                            label='After (s)',
                            value=project_settings.snippet_padding_after,
                            min=0, step=0.5, format='%.1f',
                            on_change=lambda e: _on_setting_change('snippet_padding_after', e.value),
                        ).classes('w-36').props('outlined dense')
            _cols_btn = ui.button(icon='view_column').props('flat dense round')
            _cols_btn.disable()
            with _cols_btn:
                ui.tooltip('Visible Columns')
                with ui.menu():
                    self._cols_menu_container = ui.column().classes('p-4 gap-1')
            _cols_btn.on('click', self._populate_cols_menu)
            _export_btn = ui.button(icon='download').props('flat dense round')
            _export_btn.disable()
            with _export_btn:
                ui.tooltip('Export')
                with ui.menu():
                    ui.menu_item('Export CSV', on_click=self._export_csv)
                    ui.menu_item('Export audio snippets', on_click=self._export_audio_snippets)
            self._action_buttons = [_padding_btn, _cols_btn, _export_btn]

        container = ui.column().classes('w-full flex-1')
        self._grid.attach(container)

    async def refresh(self) -> None:
        """Re-discover campaigns and reload data. Called when tab is activated."""
        audio_root = Path(project_settings.audio_recordings_path)
        self._data.discover(audio_root)
        n = len(self._data.campaign_paths)
        options = {ALL_CAMPAIGNS: f'All campaigns ({n})' if n else 'All campaigns'} | {name: name for name in self._data.campaign_paths}
        self._campaign_select.options = options
        self._campaign_select.set_value(ALL_CAMPAIGNS)
        self._campaign_select.update()
        await self._on_campaign_change(ALL_CAMPAIGNS)

    async def _on_campaign_change(self, value: str) -> None:
        if value == ALL_CAMPAIGNS:
            await self._load_all_campaigns()
        elif value and value in self._data.campaign_paths:
            await self._load_campaign(value)
        else:
            self._set_empty('No campaign selected')

    async def _load_campaign(self, name: str) -> None:
        """Load detections CSV for a single campaign."""
        output_base = get_project_output_base()
        preferred_lang = project_settings.preferred_species_lang
        result = await run.io_bound(self._data.load_campaign, name, output_base, preferred_lang)
        if result is None:
            self._set_empty('No detections found. Run BirdNET first.')
            return
        rows, fieldnames = result
        if not rows:
            self._set_empty('CSV is empty, no detections')
            return
        await self._show_detections(rows, fieldnames)

    async def _load_all_campaigns(self) -> None:
        """Load the project-level combined detections CSV."""
        project_name = get_project_name(get_project_path()) or 'project'
        output_base = get_project_output_base()
        preferred_lang = project_settings.preferred_species_lang
        result = await run.io_bound(self._data.load_all_campaigns, output_base, project_name, preferred_lang)
        if result is None:
            self._set_empty('No detections found. Run BirdNET first.')
            return
        rows, fieldnames = result
        if not rows:
            self._set_empty('No detections found. Run BirdNET first.')
            return
        await self._show_detections(rows, fieldnames)

    async def _show_detections(self, rows: list[dict], fieldnames: list[str]) -> None:
        """Store rows and display in the grid."""
        truncated = self._data.set_detections(rows, fieldnames, _MAX_ROWS)
        if truncated > 0:
            ui.notify(
                f'Showing first {_MAX_ROWS:,} of {_MAX_ROWS + truncated:,} detections. Increase "Min confidence" or select a specific campaign to reduce the amount of rows.',
                type='warning',
                timeout=10000,
            )
        self._progress_bar.set_visibility(True)
        self._progress_bar.set_value(0)
        self._status_label.set_text(f'Loading {len(rows):,} detections... 0%')
        await self._show_aggrid(fieldnames)

    def _ensure_media_route(self) -> None:
        """Register the audio recordings directory as a media route (once)."""
        if self._media_route_added:
            return
        audio_root = project_settings.audio_recordings_path
        if audio_root:
            app.add_media_files('/media', audio_root)
            self._media_route_added = True

    async def _show_aggrid(self, fieldnames: list[str]) -> None:
        """Create or update the AG Grid with loaded detections."""
        self._ensure_media_route()

        # Filter rows based on max detections per ARU/species setting
        display_rows = self._data.filter_max_per_aru_species(self._max_detections_per_aru_and_species)

        audio_root = Path(project_settings.audio_recordings_path) if project_settings.audio_recordings_path else None
        pad_before = float(project_settings.snippet_padding_before or 0)

        # Pre-compute file durations once per unique file (server-side from WAV header).
        # Path resolution is done client-side in the JS cell renderer to keep row data
        # minimal and avoid sending _rel_file annotations over WebSocket.
        unique_files = {row.get('File') for row in display_rows if row.get('File')}
        file_durations: dict[str, float] = {}
        for file_path in unique_files:
            rel = resolve_audio_path(file_path, audio_root)
            rel_path = rel.as_posix() if rel else file_path
            if rel_path not in file_durations:
                # Durations are computed server-side from WAV header (via sf.info, cached)
                abs_path = audio_root / rel if rel else None
                file_durations[rel_path] = audio_duration(abs_path) if abs_path else 0.0

        grid_context = {
            'audio_root': '/media/',
            'pad_before': pad_before,
            'file_durations': file_durations,
        }

        await self._grid.refresh(
            display_rows,
            fieldnames,
            self._data.species_options,
            self._on_cell_click,
            self._on_cell_value_changed,
            grid_context=grid_context,
            on_progress=self._update_progress,
        )

    def _update_progress(self, val: float, n: int) -> None:
        """Update the progress bar and status label during chunked grid loading.

        This callback is invoked from the async event loop, so direct UI calls
        are safe. However, we guard against None refs in case the panel is
        destroyed mid-load.
        """
        if self._progress_bar is not None:
            self._progress_bar.set_value(val)

        if val >= 1.0:
            if self._progress_bar is not None:
                self._progress_bar.set_visibility(False)
            for btn in self._action_buttons:
                btn.enable()
            status_text = f'{n:,} detection{"s" if n != 1 else ""}'
            if self._max_detections_per_aru_and_species > 0:
                status_text += f' (max {self._max_detections_per_aru_and_species} per ARU/species)'
            if self._status_label is not None:
                self._status_label.set_text(status_text)
        else:
            if self._status_label is not None:
                self._status_label.set_text(f'Loading {n:,} detections... {int(val*100)}%')

    def _populate_cols_menu(self) -> None:
        if not self._data.fieldnames or self._cols_menu_container is None:
            return
        self._grid.populate_cols_menu(self._cols_menu_container, self._data.fieldnames)

    def _on_cell_click(self, e) -> None:
        """Play audio snippet when the ♪ column is clicked."""
        if e.args.get('colId') != '_play':
            return
        row = e.args.get('data', {})
        file_path = row.get('File')
        if not file_path:
            return

        url = f'/media/{file_path}'
        pad_before = float(project_settings.snippet_padding_before or 0)
        play_start = max(0.0, float(row.get('Start_Time') or 0) - pad_before)
        end = float(row.get('End_Time', 0))
        pad_after = float(project_settings.snippet_padding_after or 0)
        total_ms = int((end + pad_after - play_start) * 1000)

        js_url = json.dumps(url)
        ui.run_javascript(f"""
            let audio = document.getElementById('examine-audio');
            if (audio && !audio.paused && window._examinePlayUrl === {js_url}) {{
                audio.pause();
                clearTimeout(window._examineStopTimer);
            }} else {{
                if (window._examineActiveAudio && !window._examineActiveAudio.paused) {{
                    window._examineActiveAudio.pause();
                    window._examineActiveBtn.textContent = '▶';
                }}
                if (!audio) {{
                    audio = document.createElement('audio');
                    audio.id = 'examine-audio';
                    document.body.appendChild(audio);
                }}
                window._examinePlayUrl = {js_url};
                audio.src = {js_url};
                audio.currentTime = {play_start};
                audio.play();
                clearTimeout(window._examineStopTimer);
                window._examineStopTimer = setTimeout(() => audio.pause(), {total_ms});
            }}
        """)

    def _on_cell_value_changed(self, e) -> None:
        """Update the edited row in detections and persist to CSV(s)."""
        data = e.args.get('data', {})
        self._data.update_cell(
            file_val=data.get('File', ''),
            start_val=data.get('Start_Time'),
            sci_val=data.get('Scientific_Name', ''),
            annotation_data=data,
        )
        output_base = get_project_output_base()
        self._data.save_detections(output_base, get_project_name(get_project_path()))

    async def _export_csv(self) -> None:
        """Export the currently filtered detection table to a user-chosen file."""
        if not self._grid.exists:
            return

        output_base = get_project_output_base()
        if self._data.current_campaign and self._data.current_campaign != ALL_CAMPAIGNS:
            default_dir = str(output_base / self._data.current_campaign)
            default_name = f'{self._data.current_campaign}-detections-export.csv'
        else:
            default_dir = str(output_base)
            default_name = f'{get_project_name(get_project_path()) or "project"}-detections-export.csv'

        result = await app.native.main_window.create_file_dialog(
            FileDialog.SAVE,
            directory=default_dir,
            save_filename=default_name,
            file_types=('CSV files (*.csv)', 'All files (*.*)'),
        )
        if not result:
            return
        save_path = Path(result if isinstance(result, str) else result[0])

        # Export only visible columns (and rows)
        export_cols = [f for f in self._data.fieldnames if f not in self._grid.hidden_extra]
        csv_string = await self._grid.run_method('getDataAsCsv', {'columnKeys': export_cols})
        if csv_string:
            save_path.write_text(csv_string, encoding='utf-8')
            ui.notify(f'Exported to {save_path.name}', type='positive')

    async def _export_audio_snippets(self) -> None:
        """Extract padded audio snippets for all currently visible detections."""
        if not self._grid.exists or not self._data.fieldnames:
            return

        result = await app.native.main_window.create_file_dialog(
            FileDialog.FOLDER,
            directory=str(get_project_output_base()),
        )
        if not result:
            return
        output_dir = Path(result if isinstance(result, str) else result[0])

        # Get displayed rows (respects floating filters) including hidden fields like File/Start_Time/End_Time
        csv_string = await self._grid.run_method('getDataAsCsv', {'columnKeys': self._data.fieldnames})
        if not csv_string:
            return

        rows = list(csv.DictReader(io.StringIO(csv_string)))
        if not rows:
            ui.notify('No rows to export', type='warning')
            return

        missing = {'File', 'Start_Time', 'End_Time'} - rows[0].keys()
        if missing:
            ui.notify(
                f'Cannot export snippets due to missing columns: {", ".join(missing)}',
                type='negative',
            )
            return

        pad_before = float(project_settings.snippet_padding_before or 0)
        pad_after = float(project_settings.snippet_padding_after or 0)
        audio_root = Path(project_settings.audio_recordings_path)
        corrected_sci_map = build_locale_reverse_map(project_settings.preferred_species_lang)

        ui.notify(f'Exporting {len(rows)} audio snippets…', type='info', timeout=3000)

        errors: list[str] = []
        for row in rows:
            file_path = row.get('File', '')
            if not file_path:
                errors.append(f'Missing file field in row: {row}')
                continue
            try:
                start = float(row.get('Start_Time') or 0)
                end = float(row.get('End_Time') or 0)
            except (ValueError, TypeError) as exc:
                errors.append(f'Bad time values in row {row}: {exc}')
                continue

            rel = resolve_audio_path(file_path, audio_root)
            if rel is None:
                errors.append(f'Cannot resolve path: {file_path}')
                continue
            src = audio_root / rel

            if not src.exists():
                errors.append(f'Audio file not found: {src}')
                continue

            play_start = max(0.0, start - pad_before)
            play_end = end + pad_after
            dst = output_dir / build_snippet_filename(row, play_start, play_end, corrected_sci_map)
            try:
                await run.io_bound(extract_audio_snippet, src, play_start, play_end, dst)
            except Exception as exc:
                errors.append(f'{src.name}: {exc}')

        n_ok = len(rows) - len(errors)
        if errors:
            for msg in errors:
                print(f'[audio export] {msg}', file=sys.stderr)
            first = errors[0]
            ui.notify(
                f'Exported {n_ok}/{len(rows)} snippets. First error: {first}',
                type='warning',
                timeout=10000,
            )
        else:
            ui.notify(f'Exported {len(rows)} snippets to {output_dir.name}', type='positive')

    async def _on_padding_before_change(self, e) -> None:
        """Persist the new padding-before value and refresh the grid (updates _play_start/_start_fraction)."""
        _on_setting_change('snippet_padding_before', e.value)
        await self._refresh_row_data()

    async def _refresh_row_data(self) -> None:
        """Recompute annotated row data and push to the grid (e.g. after padding change)."""
        if self._data.detections:
            await self._show_aggrid(self._data.fieldnames)

    async def _on_max_detections_per_aru_and_species_change(self, e) -> None:
        """Handle dropdown change for max detections per ARU/species filter."""
        self._max_detections_per_aru_and_species = 0 if e.value == 'All' else int(e.value)
        await self._refresh_row_data()

    def _set_empty(self, message: str) -> None:
        """Clear the grid and show a status message."""
        self._data.clear()
        self._progress_bar.set_visibility(False)
        self._status_label.set_text(message)
        for btn in self._action_buttons:
            btn.disable()
        self._grid.clear()
