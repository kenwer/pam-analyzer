"""QAbstractTableModel adapter over a DetectionStore.

Implements ``sort_by_priority(priority)`` so the MultiColumnSortTable's
fast-path bypasses the Qt proxy comparator on large datasets, plus
``set_column_filter`` so the
:class:`pam_analyzer.ui.detection_table.DetectionTable` can drive its
filter row, play-button delegate, and audio player.

Cells are read straight from the store's polars frame and edits go back
through DetectionStore.set_annotations, so the table, its filters and the
saved CSVs all see one copy of the data.

Column 0 is a virtual play-button column (no payload, never sortable).
The real detection fields start at column 1.
"""

from collections.abc import Callable
from typing import Any

import polars as pl
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from ...domain import Detection, DetectionStore, top_per_aru_species
from ...domain.detection_schema import COLUMNS as _SCHEMA_COLUMNS
from ...domain.detection_schema import ColumnSpec, is_locale_column
from ...domain.filter_ops import (
    ColumnFilter,
    ColumnKind,
    FilterOp,
    datetime_helper_exprs,
    default_op,
)

PLAY_COLUMN_INDEX = 0
"""The model column index reserved for the virtual play-button column."""

_PLAY_COLUMN = ColumnSpec("_play", lambda _d: "")

# The play column plus the schema's canonical column list. The schema order
# matches the column order BirdnetRunner emits when
# writing their per-model CSV, so the on-screen table is a direct
# visual analog of the file on disk. The model may extend this list at
# runtime with extras discovered in Detection.extra (e.g. Species_de /
# Species_fr from a multi-locale Perch run); those land right after the
# Species column, which is also where the runners place them in the CSV.
_STATIC_COLUMNS: tuple[ColumnSpec, ...] = (_PLAY_COLUMN, *_SCHEMA_COLUMNS)

# Header to index for the static set. Panels use this to wire delegates and
# default sort priority to known columns; dynamic extras get looked up via
# DetectionsTableModel.index_of_column instead.
COLUMNS_BY_NAME = {c.name: i for i, c in enumerate(_STATIC_COLUMNS)}

NUMERIC_COLUMNS: frozenset[int] = frozenset(i for i, c in enumerate(_STATIC_COLUMNS) if c.numeric)
"""Indices of numeric columns. Consumed by the header filter row to pick the
operator menu (number ops vs text ops). Dynamic Species_<locale> extras are
text-only, so they don't appear here."""


def _extra_column_getter(key: str) -> Callable[[Detection], Any]:
    """Build a getter that pulls *key* out of Detection.extra.

    Free function rather than a lambda so the resulting Column survives
    repr() / pickling cleanly and so each capture binds *key* explicitly
    rather than via late-binding closure quirks.
    """
    def get(d: Detection) -> Any:
        return d.extra.get(key, "")

    return get

# Ops that ignore the typed value and stay active even when the input is empty.
_BLANK_OPS: frozenset[FilterOp] = frozenset({FilterOp.BLANK, FilterOp.NOT_BLANK})

DEFAULT_HIDDEN_COLUMNS: frozenset[str] = frozenset(
    {"Start_Time", "End_Time", "Lat", "Lon", "Species_List", "Min_Conf"}
)
"""Column names hidden by default on first run (no saved state)."""

__all__ = ["COLUMNS_BY_NAME", "DEFAULT_HIDDEN_COLUMNS", "NUMERIC_COLUMNS", "PLAY_COLUMN_INDEX"]


class DetectionsTableModel(QAbstractTableModel):
    """Table model over a DetectionStore, with column filters, a max-per cap and sort."""

    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)
        # Active column list. Starts as the static set. set_store() may extend
        # it with one column per Species_<locale> column in the store frame, so
        # users can show or hide localized names without touching the CSV.
        self._columns: list[ColumnSpec] = list(_STATIC_COLUMNS)
        self._store = DetectionStore.empty()
        # Parsed date/time helper columns, row-aligned with the store frame, so
        # filtering never re-parses the ISO strings. Recording_Time is read-only.
        self._helpers: pl.DataFrame = pl.DataFrame()
        # Row ids (store frame positions) in display order, post-filter and post-sort.
        self._visible: list[int] = []
        # True at every row id in _visible, for vectorized visibility tests.
        self._visible_mask: pl.Series = pl.Series(dtype=pl.Boolean)
        # Per-column active filter, keyed by column index. Col 0 is reserved
        # for the play column and is never filtered. An entry is only present
        # when the column has an active filter (text ops with non-empty input,
        # or BLANK and NOT_BLANK regardless).
        self._col_filters: dict[int, ColumnFilter] = {}
        # Cap on rows kept per (ARU, Species), highest confidence first. 0
        # disables the cap. Applied after the column filters (filter first,
        # then cap) so the cap ranks only the rows that survive the filters.
        self._max_per: int = 0
        # Active sort priority. Re-applied after filter changes.
        self._sort_priority: list[tuple[int, Qt.SortOrder]] = []

    @property
    def store(self) -> DetectionStore:
        return self._store

    def set_store(self, store: DetectionStore) -> None:
        self.beginResetModel()
        self._store = store
        self._col_filters.clear()
        # Locale extras live next to Species rather than at the end of the
        # row, because users group them mentally with the base species name.
        # The shift makes COLUMNS_BY_NAME stale for any static column past
        # Species, so production callers must use index_of() instead.
        extras = sorted(c for c in store.frame.columns if is_locale_column(c))
        species_pos = next(
            (i for i, c in enumerate(_STATIC_COLUMNS) if c.name == "Species"),
            len(_STATIC_COLUMNS),
        )
        self._columns = [
            *_STATIC_COLUMNS[: species_pos + 1],
            *(
                ColumnSpec(h, _extra_column_getter(h), kind=ColumnKind.CATEGORICAL)
                for h in extras
            ),
            *_STATIC_COLUMNS[species_pos + 1 :],
        ]
        self._helpers = store.frame.select(
            [
                e
                for c in _SCHEMA_COLUMNS
                if c.kind is ColumnKind.DATETIME
                for e in datetime_helper_exprs(c.name)
            ]
        )
        # Filters were just cleared, so this only applies any active max-per cap
        # (a persistent user preference) to the fresh rows.
        self._rebuild_visible()
        self._apply_sort()
        self.endResetModel()

    def numeric_column_indices(self) -> set[int]:
        """Indices of currently-visible numeric columns.

        Computed from self._columns so the filter row's number-vs-text
        operator menu stays correct after extras (which are always text)
        shift the static columns.
        """
        return {i for i, c in enumerate(self._columns) if c.numeric}

    def column_kinds(self) -> dict[int, ColumnKind]:
        """Kind per current column index, driving each filter slot's op menu.

        Computed from self._columns so the mapping stays correct after
        dynamic Species_<locale> extras shift the static columns.
        """
        return {i: c.kind for i, c in enumerate(self._columns)}

    def distinct_values(self, col: int) -> list[str]:
        """Sorted distinct non-blank values of *col*, for the "is one of" popup.

        Blank cells are served by the BLANK op instead of appearing here.
        """
        if not (0 <= col < len(self._columns)) or col == PLAY_COLUMN_INDEX:
            return []
        name = self._columns[col].name
        frame = self._store.frame
        if frame.is_empty() or name not in frame.columns:
            return []
        values = frame[name].cast(pl.String).drop_nulls().unique().sort().to_list()
        return [v for v in values if v != ""]

    def index_of(self, name: str) -> int:
        """Return the current column index for *name*, or -1 if absent.

        Prefer this over the static COLUMNS_BY_NAME map in any production
        code that runs after set_store, because dynamic Species_<locale>
        extras get inserted next to Species and shift the indices of every
        static column after that.
        """
        for i, c in enumerate(self._columns):
            if c.name == name:
                return i
        return -1

    def column_names(self, *, include_play: bool = False) -> list[str]:
        """Return current column header names, in column order.

        Skips the play column by default so callers iterating "data
        columns" don't have to special-case it. Used by the panel for
        CSV export and the default-hidden-extras heuristic.
        """
        if include_play:
            return [c.name for c in self._columns]
        return [c.name for c in self._columns if c.name != "_play"]

    def row_id_at(self, visible_row: int) -> int | None:
        """Store row id shown at *visible_row*, or None when out of range."""
        if not (0 <= visible_row < len(self._visible)):
            return None
        return self._visible[visible_row]

    def visible_row_of(self, row_id: int) -> int | None:
        """Display position of *row_id*, or None when it is filtered out."""
        try:
            return self._visible.index(row_id)
        except ValueError:
            return None

    def detection_at(self, visible_row: int) -> Detection | None:
        """Snapshot of the row at *visible_row*. Edit through setData, not the snapshot."""
        row_id = self.row_id_at(visible_row)
        return None if row_id is None else self._store.detection(row_id)

    def value_at(self, visible_row: int, name: str) -> Any:
        """One cell of the row at *visible_row*, or None when out of range."""
        row_id = self.row_id_at(visible_row)
        if row_id is None or name not in self._store.frame.columns:
            return None
        return self._store.frame.get_column(name)[row_id]

    def visible_frame(self) -> pl.DataFrame:
        """The visible rows in display order, store columns only."""
        return self._store.frame.select(pl.all().gather(self._visible))

    def rows_frame(self, visible_rows: list[int]) -> pl.DataFrame:
        """The given visible rows, in the given order, store columns only."""
        ids = pl.Series([self._visible[r] for r in visible_rows], dtype=pl.UInt32)
        return self._store.frame.select(pl.all().gather(ids))

    def visible_column(self, name: str) -> pl.Series:
        """One column of the visible rows, in display order."""
        return self._store.frame.get_column(name).gather(self._visible)

    def rows_in_file(self, file: str, exclude_row_id: int) -> pl.DataFrame:
        """Visible rows from audio *file*, other than *exclude_row_id*, in store order."""
        frame = self._store.frame.with_row_index("__row")
        return frame.filter(
            pl.lit(self._visible_mask)
            & (pl.col("File") == file)
            & (pl.col("__row") != exclude_row_id)
        )

    def set_column_filter(self, col: int, text: str, op: FilterOp | None = None) -> None:
        """Apply a per-column filter using the given :class:`FilterOp`.

        When *op* is omitted, the column's natural default is used (Contains
        for text columns, Equals for numeric columns). An empty *text* with
        a value-taking op clears the filter. BLANK and NOT_BLANK stay
        active regardless of *text*.
        """
        if not (0 <= col < len(self._columns)) or col == PLAY_COLUMN_INDEX:
            return
        spec = self._columns[col]
        if op is None:
            op = default_op(spec.kind)

        text = text.strip()
        active = (op in _BLANK_OPS) or bool(text)
        if active:
            self._col_filters[col] = ColumnFilter(spec.name, op, text, spec.kind)
        else:
            self._col_filters.pop(col, None)
        self.beginResetModel()
        self._rebuild_visible()
        self._apply_sort()
        self.endResetModel()

    def has_filters(self) -> bool:
        return bool(self._col_filters)

    def clear_filters(self) -> None:
        if not self._col_filters:
            return
        self._col_filters.clear()
        self.beginResetModel()
        self._rebuild_visible()
        self._apply_sort()
        self.endResetModel()

    def set_max_per(self, n: int) -> None:
        """Cap the visible rows to the top *n* per (ARU, Species) by confidence.

        A value of 0 (or less) disables the cap. The cap composes with the
        per-column filters: filters run first, then the cap keeps the highest
        confidence rows among the survivors. Unlike a fresh set_store, this
        leaves the column filters in place, so adjusting the cap never silently
        drops an active filter.
        """
        n = max(n, 0)
        if n == self._max_per:
            return
        self._max_per = n
        self.beginResetModel()
        self._rebuild_visible()
        self._apply_sort()
        self.endResetModel()

    # QAbstractTableModel overrides

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._visible)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._columns)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> Any:
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal and 0 <= section < len(self._columns):
            header = self._columns[section].name
            # The play column is rendered by a delegate; show no header text.
            return "" if header == "_play" else header
        if orientation == Qt.Vertical:
            return section + 1
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        if role not in (Qt.DisplayRole, Qt.EditRole):
            return None
        row, col = index.row(), index.column()
        if not (0 <= row < len(self._visible) and 0 <= col < len(self._columns)):
            return None
        if col == PLAY_COLUMN_INDEX:
            return ""  # play column; delegate paints the icon
        value = self._store.frame.get_column(self._columns[col].name)[self._visible[row]]
        return "" if value is None else value

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        flags = super().flags(index)
        if not index.isValid():
            return flags
        if self._columns[index.column()].editable:
            flags |= Qt.ItemIsEditable
        return flags

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.EditRole) -> bool:
        if role != Qt.EditRole or not index.isValid():
            return False
        col = self._columns[index.column()]
        if not col.editable:
            return False
        return self.set_annotation([index.row()], col.name, "" if value is None else str(value))

    def set_annotation(self, visible_rows: list[int], name: str, value: str) -> bool:
        """Set annotation column *name* to *value* on the given visible rows.

        Emits a single dataChanged spanning the first to the last of those
        rows. Returns False, with nothing changed, for no rows, a row out of
        range, or a column or value the store rejects.
        """
        if not visible_rows:
            return False
        first, last = min(visible_rows), max(visible_rows)
        if first < 0 or last >= len(self._visible):
            return False
        try:
            self._store.set_annotations([self._visible[r] for r in visible_rows], name, value)
        except ValueError:
            return False
        col = self.index_of(name)
        self.dataChanged.emit(self.index(first, col), self.index(last, col), [Qt.DisplayRole, Qt.EditRole])
        return True

    # MultiColumnSortTable fast path

    def sort_by_priority(self, priority: list[tuple[int, Qt.SortOrder]]) -> None:
        """Sort visible rows by the proxy's (column, order) key list, oldest key last."""
        self._sort_priority = list(priority)
        self.beginResetModel()
        self._apply_sort()
        self.endResetModel()

    def _filter_frame(self) -> pl.DataFrame:
        """Store frame plus the date/time helper columns the filters read."""
        if not self._helpers.width:
            return self._store.frame
        return self._store.frame.hstack(self._helpers)

    def _rebuild_visible(self) -> None:
        """Recompute _visible from the store, the column filters, and the max-per cap.

        Column filters run first (their masks are ANDed over the frame), then
        the cap keeps the top rows per (ARU, Species) among the survivors.
        Called inside a model reset.
        """
        frame = self._filter_frame().with_row_index("__row")
        if self._col_filters:
            mask = pl.lit(True)
            for col_idx, cf in self._col_filters.items():
                if col_idx == PLAY_COLUMN_INDEX or col_idx >= len(self._columns):
                    continue
                if cf.column not in frame.columns:
                    continue
                mask = mask & cf.to_polars()
            frame = frame.filter(mask)
        frame = top_per_aru_species(frame, self._max_per)
        self._visible = frame["__row"].to_list()
        self._visible_mask = pl.repeat(False, self._store.row_count, eager=True).scatter(self._visible, True)

    def _apply_sort(self) -> None:
        """Reorder _visible according to _sort_priority. Called inside a model reset."""
        if not self._sort_priority or not self._visible:
            return

        col_names: list[str] = []
        descending: list[bool] = []
        for c, order in self._sort_priority:
            if c == PLAY_COLUMN_INDEX or c >= len(self._columns):
                continue
            col_names.append(self._columns[c].name)
            descending.append(order == Qt.SortOrder.DescendingOrder)

        if not col_names:
            return

        keys = self._store.frame.select([pl.col(n).gather(self._visible) for n in col_names])
        keys = keys.with_columns(pl.Series("__idx", self._visible))
        self._visible = keys.sort(col_names, descending=descending, nulls_last=True)["__idx"].to_list()


def _sort_key(value: Any) -> tuple[int, Any]:  # type: ignore[reportUnusedFunction]
    """Return a key that sorts None last and groups numbers/strings sensibly."""
    if value is None or value == "":
        return (1, "")
    return (0, value)
