"""QAbstractTableModel adapter over a list[Detection].

Implements ``sort_by_priority(priority)`` so the MultiColumnSortTable's
fast-path bypasses the Qt proxy comparator on large datasets, plus
``set_column_filter`` so the
:class:`pam_analyzer.widgets.detection_table.DetectionTable` can drive its
filter row, play-button delegate, and audio player.

Column 0 is a virtual play-button column (no payload, never sortable).
The real detection fields start at column 1.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import polars as pl
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from ...domain import Detection, VerifiedState
from ...widgets.filter_ops import FilterOp, default_op, to_polars_expr


@dataclass(frozen=True)
class _Column:
    header: str
    get: Callable[[Detection], Any]
    set: Callable[[Detection, str], None] | None = None  # None ⇒ read-only
    numeric: bool = False

    @property
    def editable(self) -> bool:
        return self.set is not None


def _set_verified(d: Detection, value: str) -> None:
    d.verified = VerifiedState(value or "")


def _set_corrected_species(d: Detection, value: str) -> None:
    d.corrected_species = value


def _set_comment(d: Detection, value: str) -> None:
    d.comment = value


PLAY_COLUMN_INDEX = 0
"""The model column index reserved for the virtual play-button column."""

_PLAY_COLUMN = _Column("_play", lambda _d: "")  # type: ignore[reportUnusedVariable]

# Static columns whose presence is independent of the loaded CSV. Order
# matches the column order both BirdnetRunner and PerchRunner emit when
# writing <campaign>-detections.csv, so the on-screen table is a direct
# visual analog of the file on disk. The model may extend this list at
# runtime with extras discovered in Detection.extra (e.g. Species_de /
# Species_fr from a multi-locale Perch run); those land right after the
# Species column, which is also where the runners place them in the CSV.
_STATIC_COLUMNS: tuple[_Column, ...] = (
    _PLAY_COLUMN,
    _Column("Campaign", lambda d: d.campaign),
    _Column("ARU", lambda d: d.aru),
    _Column("Start_Time", lambda d: d.start_time, numeric=True),
    _Column("End_Time", lambda d: d.end_time, numeric=True),
    _Column("Scientific_Name", lambda d: d.scientific_name),
    _Column("Species", lambda d: d.species),
    _Column("Confidence", lambda d: d.confidence, numeric=True),
    _Column("Rank", lambda d: d.rank, numeric=True),
    _Column("File", lambda d: d.file),
    _Column("Recording_Time", lambda d: d.recording_time),
    _Column("Week", lambda d: d.week, numeric=True),
    _Column("Lat", lambda d: d.lat, numeric=True),
    _Column("Lon", lambda d: d.lon, numeric=True),
    _Column("Species_List", lambda d: d.species_list),
    _Column("Min_Conf", lambda d: d.min_conf, numeric=True),
    _Column("Model", lambda d: d.model),
    _Column("Verified", lambda d: d.verified.value, _set_verified),
    _Column("Corrected_Species", lambda d: d.corrected_species, _set_corrected_species),
    _Column("Comment", lambda d: d.comment, _set_comment),
)

# Header to index for the static set. Panels use this to wire delegates and
# default sort priority to known columns; dynamic extras get looked up via
# DetectionsTableModel.index_of_column instead.
COLUMNS_BY_NAME = {c.header: i for i, c in enumerate(_STATIC_COLUMNS)}

# Static-column getters. Extras are read via DetectionsTableModel.column_getter,
# which falls back to a closure over Detection.extra.
COLUMN_GETTERS: dict[str, Callable[[Detection], Any]] = {c.header: c.get for c in _STATIC_COLUMNS}

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

__all__ = ["COLUMN_GETTERS", "COLUMNS_BY_NAME", "DEFAULT_HIDDEN_COLUMNS", "NUMERIC_COLUMNS", "PLAY_COLUMN_INDEX"]


class DetectionsTableModel(QAbstractTableModel):
    """Mutable model owning a list of Detection rows."""

    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)
        # Active column list. Starts as the static set; set_detections() may
        # extend it with one column per Species_<locale> key discovered in
        # Detection.extra so users can show/hide localized names in the
        # examine panel without touching the source CSV.
        self._columns: list[_Column] = list(_STATIC_COLUMNS)
        self._all: list[Detection] = []
        # Indices into _all in display order (post-filter, post-sort).
        self._visible: list[int] = []
        # Detections that have been edited but not yet saved by the panel.
        # Tracked by id() so sorting/filtering doesn't invalidate the set.
        self._dirty_ids: set[int] = set()
        # Per-column active filter as (raw text, FilterOp).
        # Col 0 is reserved for the play column and is never filtered.
        # An entry is only present when the column has an active filter
        # (text ops with non-empty input, or BLANK and NOT_BLANK regardless).
        self._col_filters: dict[int, tuple[str, FilterOp]] = {}
        # Active sort priority. Re-applied after filter changes.
        self._sort_priority: list[tuple[int, Qt.SortOrder]] = []
        # Polars DataFrame mirroring _all for filter/sort index computation.
        self._sort_df: pl.DataFrame = pl.DataFrame()

    def set_detections(self, rows: list[Detection]) -> None:
        self.beginResetModel()
        self._all = list(rows)
        self._dirty_ids.clear()
        self._col_filters.clear()
        # Locale extras live next to Species rather than at the end of the
        # row, because users group them mentally with the base species name.
        # The shift makes COLUMNS_BY_NAME stale for any static column past
        # Species, so production callers must use index_of() instead.
        extras = sorted({k for d in rows for k in d.extra if k.startswith("Species_")})
        species_pos = next(
            (i for i, c in enumerate(_STATIC_COLUMNS) if c.header == "Species"),
            len(_STATIC_COLUMNS),
        )
        self._columns = [
            *_STATIC_COLUMNS[: species_pos + 1],
            *(_Column(h, _extra_column_getter(h)) for h in extras),
            *_STATIC_COLUMNS[species_pos + 1 :],
        ]
        self._sort_df = self._build_sort_df(self._all)
        self._visible = list(range(len(self._all)))
        self._apply_sort()
        self.endResetModel()

    def numeric_column_indices(self) -> set[int]:
        """Indices of currently-visible numeric columns.

        Computed from self._columns so the filter row's number-vs-text
        operator menu stays correct after extras (which are always text)
        shift the static columns.
        """
        return {i for i, c in enumerate(self._columns) if c.numeric}

    def index_of(self, name: str) -> int:
        """Return the current column index for *name*, or -1 if absent.

        Prefer this over the static COLUMNS_BY_NAME map in any production
        code that runs after set_detections, because dynamic Species_<locale>
        extras get inserted next to Species and shift the indices of every
        static column after that.
        """
        for i, c in enumerate(self._columns):
            if c.header == name:
                return i
        return -1

    def column_names(self, *, include_play: bool = False) -> list[str]:
        """Return current column header names, in column order.

        Skips the play column by default so callers iterating "data
        columns" don't have to special-case it. Used by the panel for
        CSV export and the default-hidden-extras heuristic.
        """
        if include_play:
            return [c.header for c in self._columns]
        return [c.header for c in self._columns if c.header != "_play"]

    def column_getter(self, name: str) -> Callable[[Detection], Any] | None:
        """Resolve a column header to its getter, including dynamic extras.

        Falls back through the static COLUMN_GETTERS map so existing
        callers that look up known column names still hit the same
        function objects.
        """
        for c in self._columns:
            if c.header == name:
                return c.get
        return COLUMN_GETTERS.get(name)

    def detections(self) -> list[Detection]:
        """Return currently visible (post-filter) detections in display order."""
        return [self._all[i] for i in self._visible]

    def take_dirty(self) -> list[Detection]:
        """Return the modified rows (in original-insert order) and clear the dirty set."""
        rows = [d for d in self._all if id(d) in self._dirty_ids]
        self._dirty_ids.clear()
        return rows

    def detection_at(self, visible_row: int) -> Detection | None:
        """Return the :class:`Detection` at *visible_row* (0-based in the visible/sorted view).

        Returns ``None`` if the row index is out of bounds.
        """
        if not (0 <= visible_row < len(self._visible)):
            return None
        return self._all[self._visible[visible_row]]

    def set_column_filter(self, col: int, text: str, op: FilterOp | None = None) -> None:
        """Apply a per-column filter using the given :class:`FilterOp`.

        When *op* is omitted, the column's natural default is used (Contains
        for text columns, Equals for numeric columns). An empty *text* with
        a value-taking op clears the filter. BLANK and NOT_BLANK stay
        active regardless of *text*.
        """
        if not (0 <= col < len(self._columns)) or col == PLAY_COLUMN_INDEX:
            return
        if op is None:
            op = default_op(self._columns[col].numeric)

        text = text.strip()
        active = (op in _BLANK_OPS) or bool(text)
        if active:
            self._col_filters[col] = (text, op)
        else:
            self._col_filters.pop(col, None)
        self.beginResetModel()
        self._rebuild_visible()
        self._apply_sort()
        self.endResetModel()

    def clear_filters(self) -> None:
        if not self._col_filters:
            return
        self._col_filters.clear()
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
            header = self._columns[section].header
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
        d = self._all[self._visible[row]]
        value = self._columns[col].get(d)
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
        row = index.row()
        if not (0 <= row < len(self._visible)):
            return False
        d = self._all[self._visible[row]]
        try:
            col.set(d, "" if value is None else str(value))
        except ValueError:
            return False
        self._dirty_ids.add(id(d))

        # Keep _sort_df in sync so future filter/sort on editable columns is correct.
        col_name = col.header
        if not self._sort_df.is_empty() and col_name in self._sort_df.columns:
            actual_idx = self._visible[row]
            new_val = col.get(d)  # use getter so VerifiedState becomes .value str
            self._sort_df = self._sort_df.with_columns(
                pl.when(pl.int_range(pl.len()) == actual_idx)
                .then(pl.lit("" if new_val is None else str(new_val)))
                .otherwise(pl.col(col_name))
                .alias(col_name)
            )

        self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.EditRole])
        return True

    # MultiColumnSortTable fast path

    def sort_by_priority(self, priority: list[tuple[int, Qt.SortOrder]]) -> None:
        """Sort visible rows by the proxy's (column, order) key list, oldest key last."""
        self._sort_priority = list(priority)
        self.beginResetModel()
        self._apply_sort()
        self.endResetModel()

    def _build_sort_df(self, detections: list[Detection]) -> pl.DataFrame:
        if not detections:
            return pl.DataFrame()
        data: dict[str, list] = {
            "Campaign":         [d.campaign for d in detections],
            "ARU":              [d.aru for d in detections],
            "Week":             [d.week for d in detections],
            "Species":          [d.species for d in detections],
            "Scientific_Name":  [d.scientific_name for d in detections],
            "Confidence":       [d.confidence for d in detections],
            "Start_Time":       [d.start_time for d in detections],
            "End_Time":         [d.end_time for d in detections],
            "Rank":             [d.rank for d in detections],
            "File":             [d.file for d in detections],
            "Recording_Time":   [d.recording_time for d in detections],
            "Lat":              [d.lat for d in detections],
            "Lon":              [d.lon for d in detections],
            "Species_List":     [d.species_list for d in detections],
            "Min_Conf":         [d.min_conf for d in detections],
            "Model":            [d.model for d in detections],
            # Editable columns, included so filter/sort work on them too
            "Verified":         [d.verified.value for d in detections],
            "Corrected_Species":[d.corrected_species for d in detections],
            "Comment":          [d.comment for d in detections],
        }
        # Dynamic extra columns (all str, may be absent per row so None)
        extra_keys: set[str] = set()
        for d in detections:
            extra_keys.update(d.extra.keys())
        for key in sorted(extra_keys):
            data[key] = [d.extra.get(key) for d in detections]
        return pl.DataFrame(data)

    def _rebuild_visible(self) -> None:
        """Recompute _visible from _all and _col_filters. Called inside a model reset."""
        if not self._col_filters or self._sort_df.is_empty():
            self._visible = list(range(len(self._all)))
            return

        mask = pl.lit(True)
        for col_idx, (text, op) in self._col_filters.items():
            if col_idx == PLAY_COLUMN_INDEX or col_idx >= len(self._columns):
                continue
            col_name = self._columns[col_idx].header
            if col_name not in self._sort_df.columns:
                continue
            mask = mask & to_polars_expr(col_name, text, op, self._columns[col_idx].numeric)

        self._visible = self._sort_df.with_row_index("__idx").filter(mask)["__idx"].to_list()

    def _apply_sort(self) -> None:
        """Reorder _visible according to _sort_priority. Called inside a model reset."""
        if not self._sort_priority or not self._visible or self._sort_df.is_empty():
            return

        col_names: list[str] = []
        descending: list[bool] = []
        for c, order in self._sort_priority:
            if c == PLAY_COLUMN_INDEX or c >= len(self._columns):
                continue
            col_names.append(self._columns[c].header)
            descending.append(order == Qt.SortOrder.DescendingOrder)

        if not col_names:
            return

        visible_df = self._sort_df[self._visible].with_columns(pl.Series("__idx", self._visible))
        self._visible = visible_df.sort(col_names, descending=descending, nulls_last=True)["__idx"].to_list()


def _sort_key(value: Any) -> tuple[int, Any]:  # type: ignore[reportUnusedFunction]
    """Return a key that sorts None last and groups numbers/strings sensibly."""
    if value is None or value == "":
        return (1, "")
    return (0, value)
