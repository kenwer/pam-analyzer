"""AG Grid component for the Examine panel.

Encapsulates column definitions, grid construction, and column visibility state.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path

from nicegui import ui

from pam_analyzer.core.detections_io import NUMERIC_FIELDS

# Chunk size for grid row updates. Chosen to stay under WebSocket/WebView2
# message size limits (~1-2 MB) while minimizing round-trips for large datasets.
CHUNK_SIZE = 20000

# Delay after hiding container to let the browser process the visibility change
# before the heavy grid update starts. Prevents flicker of partial data.
CHUNKED_LOAD_DELAY = 0.5

# Delay between chunks to keep UI responsive and heartbeats healthy.
CHUNK_INTERVAL_DELAY = 0.1

# Columns hidden from the grid by default (internal / run-context fields)
HIDDEN_FIELDS: frozenset[str] = frozenset(
    {'Start_Time', 'End_Time', 'Lat', 'Lon', 'Species_List', 'Min_Conf', 'Model'}
)

# JS cell renderer for the File column: loaded from static file for proper syntax highlighting
_AUDIOFILE_CELL_RENDERER = (
    Path(__file__).resolve().parent.parent / 'static' / 'audio_cell_renderer.js'
).read_text(encoding='utf-8')

# Default sort applied to detection columns
_DEFAULT_SORT: dict[str, dict] = {
    'ARU': {'sort': 'asc', 'sortIndex': 0},
    'Species': {'sort': 'asc', 'sortIndex': 1},
    'Confidence': {'sort': 'desc', 'sortIndex': 2},
}

# Columns that use flex layout and must be excluded from autoSizeColumns
_FLEX_COLUMNS = {'Comment'}

# Per-column overrides applied to detection columns
_COLUMN_OVERRIDES: dict[str, dict] = {
    'File': {
        'width': 200,
        ':cellRenderer': _AUDIOFILE_CELL_RENDERER,
    },
}

# Play button column prepended to every grid.
# Uses valueGetter instead of field so the ♪ symbol is computed client-side
# from the row data — no server-side annotation or extra payload needed.
_PLAY_COL = {
    'headerName': '',
    'colId': '_play',
    'width': 30,
    'maxWidth': 30,
    'filter': False,
    'sortable': False,
    'resizable': False,
    'valueGetter': 'data.File ? "♪" : ""',
}


def _column_defs(
    fieldnames: list[str],
    species_options: list[str] | None = None,
    hidden_extra: set[str] | None = None,
) -> list[dict]:
    """Build AG Grid columnDefs from CSV header, hiding internal fields."""
    hidden_extra = hidden_extra or set()
    annotation_config: dict[str, dict] = {
        'Verified': {
            'width': 110,
            'editable': True,
            'cellEditor': 'agSelectCellEditor',
            'cellEditorParams': {'values': ['', 'true', 'false', 'uncertain']},
        },
        'Corrected_Species': {
            'editable': True,
            **(
                {
                    'cellEditor': 'agSelectCellEditor',
                    'cellEditorParams': {'values': ['', *species_options]},
                }
                if species_options
                else {}
            ),
        },
        'Comment': {
            'flex': 1,
            'editable': True,
        },
    }

    defs: list[dict] = [_PLAY_COL]
    for name in fieldnames:
        if name == '_play' or name.startswith('_'):  # Skip internal display fields
            continue
        col: dict = {
            'field': name,
            'sortable': True,
            'resizable': True,
            'hide': name in hidden_extra,
        }
        col['filter'] = 'agNumberColumnFilter' if name in NUMERIC_FIELDS else 'agTextColumnFilter'
        if name in _DEFAULT_SORT:
            col.update(_DEFAULT_SORT[name])
        if name in _COLUMN_OVERRIDES:
            col.update(_COLUMN_OVERRIDES[name])
        if name in annotation_config:
            col.update(annotation_config[name])
        defs.append(col)
    return defs


class DetectionsGrid:
    """AG Grid wrapper for the detections table.

    Owns the aggrid element, column definitions, and column visibility state.
    """

    def __init__(self) -> None:
        self._aggrid: ui.aggrid | None = None
        self._container: ui.column | None = None
        self._last_fieldnames: list[str] = []
        self.hidden_extra: set[str] = set(HIDDEN_FIELDS)
        # Generation counter that is incremented each time _set_rows starts.
        # Used to guard against stale updates from concurrent or cancelled loads.
        self._set_rows_gen: int = 0
        # Generation that last hid the container.
        # Used to restore visibility only when the most recent load finishes.
        self._container_hide_gen: int | None = None

    def attach(self, container: ui.column) -> None:
        """Store the container that the grid will be built inside."""
        self._container = container

    @property
    def exists(self) -> bool:
        return self._aggrid is not None

    async def refresh(
        self,
        display_rows: list[dict],
        fieldnames: list[str],
        species_options: list[str],
        on_cell_click: Callable,
        on_cell_value_changed: Callable,
        grid_context: dict | None = None,
        on_progress: Callable[[float, int], None] | None = None,
        on_grid_rendered: Callable | None = None,
    ) -> None:
        """Create or update the AG Grid with new data.

        If the schema (fieldnames) is unchanged, only row data is refreshed,
        preserving user filter, sort, and column visibility state.
        Large datasets are loaded in chunks to avoid blocking the UI.
        """
        if self._aggrid is not None and fieldnames == self._last_fieldnames:
            if grid_context:
                # Fire-and-forget: context is metadata for cell renderers,
                # slight delay is harmless. Avoids TimeoutError on Windows
                # where file_durations dict can be large.
                self._fire_and_forget(self._aggrid, 'setGridOption', 'context', grid_context)
            await self._set_rows(display_rows, on_progress)
            return

        self._last_fieldnames = list(fieldnames)

        # Auto-size visible data columns; exclude flex and override columns
        name_col_ids = [
            f for f in fieldnames
                if f not in self.hidden_extra and f not in _FLEX_COLUMNS and f not in _COLUMN_OVERRIDES
        ]

        self._container.clear()
        with self._container:
            self._aggrid = ui.aggrid(
                {
                    'columnDefs': _column_defs(fieldnames, species_options, self.hidden_extra),
                    'rowData': [],
                    'context': grid_context or {},
                    'defaultColDef': {
                        'minWidth': 90,
                        'filter': True,
                        'floatingFilter': True,
                    },
                    'rowBuffer': 20,
                    'pagination': False,
                },
                theme='balham',
                auto_size_columns=False,
            ).classes('w-full h-full')
            self._aggrid.on('cellClicked', on_cell_click)
            self._aggrid.on('cellValueChanged', on_cell_value_changed)
            if on_grid_rendered:
                self._aggrid.on('renderedGridChanged', on_grid_rendered)

            if name_col_ids:
                self._aggrid.on(
                    'gridReady',
                    lambda _: self._aggrid.run_grid_method('autoSizeColumns', name_col_ids),
                )
            await self._set_rows(display_rows, on_progress)

    async def _set_rows(self, rows: list[dict], on_progress: Callable[[float, int], None] | None = None) -> None:
        """Update grid data using chunked transactions for large datasets.

        Standard NiceGUI row updates send the entire dataset as a single JSON payload,
        which can stall the UI or exceed WebSocket/WebView2 message size limits on Windows
        for hundreds of thousands of rows. This method uses chunked 'applyTransaction'
        calls to stay under those limits and yields to the event loop between chunks
        to keep the UI responsive and heartbeats healthy.

        run_grid_method is intentionally NOT awaited for row updates: this fire-and-forget
        path sends the message without waiting for a JS response, avoiding TimeoutErrors
        on large payloads. Socket.IO guarantees message ordering.
        """
        aggrid = self._aggrid
        if not aggrid:
            return

        self._set_rows_gen += 1
        gen = self._set_rows_gen
        num_rows = len(rows)

        if on_progress:
            on_progress(0.0, num_rows)

        if num_rows <= CHUNK_SIZE:
            # Small sets: update rowData directly. Fire-and-forget to avoid timeout.
            self._fire_and_forget(aggrid, 'setGridOption', 'rowData', rows)
            if on_progress:
                on_progress(1.0, num_rows)
            return

        # Hide container while chunked loading to avoid flicker.
        # Use generation-based tracking so visibility is restored only when
        # the load that hid the container finishes (not by a newer load).
        self._container_hide_gen = gen
        if self._container:
            self._container.set_visibility(False)

        try:
            # Give the browser time to process the visibility change before
            # the heavy grid update starts. Prevents flicker of partial data.
            await asyncio.sleep(CHUNKED_LOAD_DELAY)

            # Check if this load was superseded before starting heavy work
            if self._set_rows_gen != gen:
                return

            # Send first chunk immediately (fire-and-forget)
            self._fire_and_forget(aggrid, 'setGridOption', 'rowData', rows[:CHUNK_SIZE])

            await self._send_remaining_chunks(aggrid, gen, rows, CHUNK_SIZE, on_progress, num_rows)
        finally:
            # Restore visibility only if this is still the active generation
            if self._container and self._container_hide_gen == gen:
                self._container.set_visibility(True)
                self._container_hide_gen = None

    def _fire_and_forget(self, aggrid: ui.aggrid, method: str, *args) -> None:
        """Send a grid method call without waiting for a response.

        This avoids TimeoutErrors on large payloads. Socket.IO guarantees
        message ordering, so calls arrive in the correct sequence.
        """
        aggrid.run_grid_method(method, *args)

    async def _send_remaining_chunks(
        self,
        aggrid: ui.aggrid,
        gen: int,
        rows: list[dict],
        chunk_size: int,
        on_progress: Callable[[float, int], None] | None,
        num_rows: int,
    ) -> None:
        """Send remaining chunks after the first one has been dispatched."""
        total_chunks = (num_rows + chunk_size - 1) // chunk_size
        for chunk_idx in range(1, total_chunks):
            if self._set_rows_gen != gen:
                return

            start_idx = chunk_idx * chunk_size
            chunk = rows[start_idx:start_idx + chunk_size]
            self._fire_and_forget(aggrid, 'applyTransaction', {'add': chunk})

            if on_progress:
                on_progress(chunk_idx / total_chunks, num_rows)
            await asyncio.sleep(CHUNK_INTERVAL_DELAY)

        # Finalize only if this load is still active
        if self._set_rows_gen == gen:
            self._fire_and_forget(aggrid, 'redrawRows')
            if on_progress:
                on_progress(1.0, num_rows)

    def populate_cols_menu(self, menu_container: ui.column, fieldnames: list[str]) -> None:
        """Fill the column-visibility menu with checkboxes."""
        menu_container.clear()
        with menu_container:
            ui.label('Visible Columns').classes('text-subtitle2 font-bold mb-1')
            with ui.scroll_area().style('max-height: 60vh; min-width: 180px'):
                with ui.column().classes('gap-1'):
                    for field in fieldnames:
                        ui.checkbox(
                            field,
                            value=field not in self.hidden_extra,
                            on_change=lambda e, f=field: self.toggle_column_visibility(f, e.value),
                        )

    async def toggle_column_visibility(self, field: str, visible: bool) -> None:
        """Show or hide a column, updating internal state and the live grid."""
        if visible:
            self.hidden_extra.discard(field)
        else:
            self.hidden_extra.add(field)
        if self._aggrid:
            await self._aggrid.run_grid_method('setColumnsVisible', [field], visible)

    async def run_method(self, method: str, *args):
        """Proxy for aggrid.run_grid_method; returns None if no grid exists."""
        if self._aggrid:
            return await self._aggrid.run_grid_method(method, *args)

    def clear(self) -> None:
        """Remove the grid and reset schema tracking."""
        self._last_fieldnames = []
        if self._container:
            self._container.clear()
        self._aggrid = None
