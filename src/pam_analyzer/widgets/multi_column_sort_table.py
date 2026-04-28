from __future__ import (
    annotations,  # Remove once Python 3.14 becomes the minimal supported version (see PEP 649)
)

from collections.abc import Iterable

from PySide6.QtCore import (
    QAbstractItemModel,
    QModelIndex,
    QObject,
    QSize,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QApplication,
    QHeaderView,
    QTableView,
    QWidget,
)

# SortKey: a (column_index, sort_order) pair used throughout this module.
type SortKey = tuple[int, Qt.SortOrder]

_ARROWS: dict[Qt.SortOrder, str] = {Qt.AscendingOrder: "↑", Qt.DescendingOrder: "↓"}


def _toggled(order: Qt.SortOrder) -> Qt.SortOrder:
    """Toggle order between ascending and descending."""
    return Qt.DescendingOrder if order == Qt.AscendingOrder else Qt.AscendingOrder


class _MultiSortProxy(QSortFilterProxyModel):
    """Internal proxy model that sorts rows by an ordered list of (column, order) keys.

    For sorted columns the horizontal headerData is decorated with the rank
    and direction (e.g. ``"Confidence  1↓"``), so callers don't need any
    custom header painting to surface the multi-column sort state.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._priority: list[SortKey] = []

    def setSortPriority(self, priority: list[SortKey]) -> None:
        """Set the sort keys and trigger a re-sort.

        *priority* is a list of ``(column, order)`` tuples ordered by
        descending priority.  An empty list clears all sort keys.

        Fast path: if the underlying data model exposes a
        ``sort_by_priority(priority)`` method, sorting is delegated there
        (e.g. an in-place Python ``list.sort``).  This avoids the overhead
        of Qt calling ``lessThan`` O(n log n) times via the C++/Python
        boundary for large datasets.  The proxy is then put in pass-through
        mode (``sort(-1)``) so it does not re-sort the already-ordered data.

        Fallback: standard Qt proxy sort via ``lessThan``.
        """
        old_cols = {c for c, _ in self._priority}
        self._priority = list(priority)
        new_cols = {c for c, _ in self._priority}

        data_model = self._data_model()
        if hasattr(data_model, "sort_by_priority"):
            data_model.sort_by_priority(priority)
            self.sort(-1)  # data already ordered; proxy passes through
        else:
            # Fallback: Qt proxy sort via lessThan.
            if priority:
                self.sort(0, Qt.AscendingOrder)
                self.invalidate()
            else:
                self.sort(-1)

        # Repaint the header for any column whose decoration changed
        # (newly sorted, newly unsorted, or rank/direction shifted).
        affected = old_cols | new_cols
        for col in affected:
            self.headerDataChanged.emit(Qt.Horizontal, col, col)

    def _data_model(self):
        """Walk through any proxy chain to return the underlying data model."""
        src = self.sourceModel()
        while src is not None:
            inner = getattr(src, "sourceModel", lambda: None)()
            if inner is None:
                return src
            src = inner
        return src

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        """Return the header label, decorated with the sort rank for sorted columns.

        Vertical headers return sequential 1-based row numbers in display
        order. Horizontal headers append ``"  {rank}{arrow}"`` to the source
        label when the column is part of the active sort priority, and
        report a left-aligned text alignment so the title text stays put
        when the suffix is added or removed (matches AG Grid).
        """
        if orientation == Qt.Vertical and role == Qt.DisplayRole:
            return section + 1
        if orientation == Qt.Horizontal:
            if role == Qt.DisplayRole:
                base = super().headerData(section, orientation, role)
                for rank, (col, order) in enumerate(self._priority):
                    if col == section:
                        suffix = f"{rank + 1}{_ARROWS[order]}"
                        return f"{base}  {suffix}" if base else suffix
                return base
            if role == Qt.TextAlignmentRole:
                return int(Qt.AlignLeft | Qt.AlignVCenter)
        return super().headerData(section, orientation, role)

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        """Return whether *left* should sort before *right*.

        Iterates over the configured sort keys in priority order.  Numeric
        columns are compared as floats; everything else is compared as
        strings.  ``None`` values sort before any other value.
        """
        src = self.sourceModel()
        for col, order in self._priority:
            ldata = src.data(src.index(left.row(), col), Qt.DisplayRole)
            rdata = src.data(src.index(right.row(), col), Qt.DisplayRole)
            if ldata is None and rdata is None:
                cmp = 0
            elif ldata is None:
                cmp = -1
            elif rdata is None:
                cmp = 1
            else:
                try:
                    cmp = (float(ldata) > float(rdata)) - (float(ldata) < float(rdata))
                except (ValueError, TypeError):
                    cmp = (str(ldata) > str(rdata)) - (str(ldata) < str(rdata))
            if cmp != 0:
                return (cmp < 0) if order == Qt.AscendingOrder else (cmp > 0)
        return False


class _MultiSortHeaderView(QHeaderView):
    """Horizontal header that reserves space at the bottom for a filter row
    and a fixed-width slot at the right of every section for the sort-rank
    suffix (e.g. ``"  9↓"``).

    Sort priority is surfaced via the proxy's decorated headerData, so this
    view does no badge painting. The slot reservation keeps section widths
    stable across sort changes (no width jitter when a column gains or
    loses its suffix).
    """

    shown = Signal()

    # Worst-case suffix the proxy can append. Used as the width-reservation
    # template since column ranks never go beyond single digits in practice.
    _SUFFIX_TEMPLATE = "  9↓"

    def __init__(self, orientation: Qt.Orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self._filter_height: int = 0
        self.setSectionsClickable(True)
        self.setSortIndicatorShown(False)

    def setFilterHeight(self, height: int) -> None:
        """Reserve *height* pixels at the bottom of the header for a filter row."""
        self._filter_height = height
        self.updateGeometries()
        self.updateGeometry()

    def updateGeometries(self) -> None:
        self.setViewportMargins(0, 0, 0, self._filter_height)
        super().updateGeometries()

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        return QSize(hint.width(), hint.height() + self._filter_height)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.shown.emit()

    def sectionSizeFromContents(self, logical_index: int) -> QSize:
        """Add the worst-case sort-suffix width to every section.

        Without this reservation, a column sized to its plain header text
        gets clipped the moment a sort priority decorates that text. We
        subtract whatever decoration is currently part of ``super()``'s
        result so sorted columns don't double-count.
        """
        base = super().sectionSizeFromContents(logical_index)
        proxy = self.model()
        if proxy is None:
            return base
        get_source = getattr(proxy, "sourceModel", None)
        src_model = get_source() if callable(get_source) else None
        if src_model is None:
            return base
        decorated = str(proxy.headerData(logical_index, Qt.Horizontal, Qt.DisplayRole) or "")
        plain = str(src_model.headerData(logical_index, Qt.Horizontal, Qt.DisplayRole) or "")
        fm = self.fontMetrics()
        reservation = fm.horizontalAdvance(self._SUFFIX_TEMPLATE)
        already_counted = fm.horizontalAdvance(decorated) - fm.horizontalAdvance(plain)
        extra = max(0, reservation - already_counted)
        if extra == 0:
            return base
        return QSize(base.width() + extra, base.height())


class MultiColumnSortTable(QTableView):
    """A :class:`QTableView` with built-in multi-column sort support.

    The widget manages its own internal proxy model and custom header, so
    callers interact with it like any other table view. Use
    :meth:`setSourceModel` instead of ``setModel``.

    Interaction
    -----------
    - **Click** a column header to sort by that column (ascending first).
      Clicking the same column again toggles the direction.
    - **Shift-click** a column header to add it as the next sort key without
      clearing the existing ones. Shift-clicking an already-active column
      toggles its direction in place.
    - Each active sort column shows its rank and direction inline in the
      header label, e.g. ``"Confidence  1↓"``.

    Signals
    -------
    sortPriorityChanged(priority: list[tuple[int, Qt.SortOrder]])
        Emitted after every sort change. *priority* is ordered from the
        highest- to the lowest-priority key. It is empty when all sort
        keys have been cleared.

    Example
    -------
    ::

        model = QStandardItemModel()
        # populate model

        table = MultiColumnSortTable()
        table.setSourceModel(model)
        table.sortPriorityChanged.connect(on_sort_changed)
    """

    sortPriorityChanged = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._proxy = _MultiSortProxy(self)
        super().setModel(self._proxy)

        self._header = _MultiSortHeaderView(Qt.Horizontal, self)
        self.setHorizontalHeader(self._header)
        self._header.setSectionsMovable(True)
        self._header.sectionClicked.connect(self._on_header_clicked)

        self._priority: list[SortKey] = []
        self._non_sortable: frozenset[int] = frozenset()

    def setSourceModel(self, model: QAbstractItemModel) -> None:
        """Set the source data model.

        Pass a :class:`QAbstractItemModel` directly. Do not wrap it in a
        proxy first, since this widget manages its own internal proxy.
        """
        self._proxy.setSourceModel(model)

    def sourceModel(self) -> QAbstractItemModel | None:
        """Return the source model set via :meth:`setSourceModel`."""
        return self._proxy.sourceModel()

    def setNonSortableColumns(self, cols: Iterable[int]) -> None:
        """Mark columns that header clicks should never sort.

        Useful for virtual columns (e.g. a play-button column) that carry
        no sortable data.
        """
        self._non_sortable = frozenset(cols)

    def sortPriority(self) -> list[SortKey]:
        """Return the current sort keys as an ordered list of ``(column, order)`` tuples.

        The list is ordered from the highest- to the lowest-priority key.
        Returns an empty list when no sort is active.
        """
        return list(self._priority)

    def setSortPriority(self, priority: list[SortKey]) -> None:
        """Programmatically set the sort priority.

        *priority* is a list of ``(column, order)`` tuples ordered from the
        highest- to the lowest-priority key.  Pass an empty list to clear all
        sort keys.
        """
        self._priority = list(priority)
        self._apply_sort()

    def clearSort(self) -> None:
        """Remove all sort keys and restore the table to its natural row order."""
        self._priority = []
        self._apply_sort()

    def mapToSourceRow(self, proxy_row: int) -> int:
        """Return the source-model row index for a row in the view."""
        src_idx = self._proxy.mapToSource(self._proxy.index(proxy_row, 0))
        return src_idx.row()

    def _on_header_clicked(self, logical_index: int) -> None:
        """Apply a sort key when a header section is clicked.

        Plain click: sort by this column (ascending first).  Clicking the
        same column again toggles direction.

        Shift-click: add this column as an additional sort key, or toggle
        its direction if already present.
        """
        if logical_index in self._non_sortable:
            return
        modifiers = QApplication.keyboardModifiers()
        existing = next(
            (i for i, (col, _) in enumerate(self._priority) if col == logical_index),
            None,
        )

        if modifiers & Qt.ShiftModifier:
            # Shift+click: add column, or toggle direction if already present.
            if existing is not None:
                col, order = self._priority[existing]
                self._priority[existing] = (col, _toggled(order))
            else:
                self._priority.append((logical_index, Qt.AscendingOrder))
        else:
            # Plain click: single-column sort. Toggle direction if already the sole key.
            if existing == 0 and len(self._priority) == 1:
                col, order = self._priority[0]
                self._priority = [(col, _toggled(order))]
            else:
                self._priority = [(logical_index, Qt.AscendingOrder)]

        self._apply_sort()

    def _apply_sort(self) -> None:
        """Apply the current priority list to the proxy and notify listeners."""
        self._proxy.setSortPriority(self._priority)
        self.sortPriorityChanged.emit(list(self._priority))
