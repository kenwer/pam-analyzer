"""AG Grid component for the Examine panel.

Encapsulates column definitions, grid construction, and column visibility state.
"""

from collections.abc import Callable
from pathlib import Path

from nicegui import ui

from pam_analyzer.core.detections_io import NUMERIC_FIELDS

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

# Play button column prepended to every grid
_PLAY_COL = {
    'headerName': '',
    'field': '_play',
    'width': 30,
    'maxWidth': 30,
    'filter': False,
    'sortable': False,
    'resizable': False,
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
        if name == '_play':
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

    def attach(self, container: ui.column) -> None:
        """Store the container that the grid will be built inside."""
        self._container = container

    @property
    def exists(self) -> bool:
        return self._aggrid is not None

    def refresh(
        self,
        display_rows: list[dict],
        fieldnames: list[str],
        species_options: list[str],
        on_cell_click: Callable,
        on_cell_value_changed: Callable,
    ) -> None:
        """Create or update the AG Grid with new data.

        If the schema (fieldnames) is unchanged, only row data is refreshed,
        preserving user filter, sort, and column visibility state.
        """
        if self._aggrid is not None and fieldnames == self._last_fieldnames:
            self._aggrid.run_grid_method('setGridOption', 'rowData', display_rows)
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
                    'rowData': display_rows,
                    'defaultColDef': {
                        'minWidth': 90,
                        'filter': True,
                        'floatingFilter': True,
                    },
                    'rowBuffer': 20,
                },
                theme='balham',
                auto_size_columns=False,
            ).classes('w-full h-full')
            self._aggrid.on('cellClicked', on_cell_click)
            self._aggrid.on('cellValueChanged', on_cell_value_changed)
            if name_col_ids:
                self._aggrid.on(
                    'gridReady',
                    lambda _: self._aggrid.run_grid_method('autoSizeColumns', name_col_ids),
                )

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
