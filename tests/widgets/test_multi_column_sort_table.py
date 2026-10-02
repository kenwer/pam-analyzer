from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from pam_analyzer.widgets.multi_column_sort_table import MultiColumnSortTable


class _CountingModel(QAbstractTableModel):
    """Table model that counts how often Qt asks for an item's flags."""

    def __init__(self, rows: int, columns: int) -> None:
        super().__init__()
        self._rows = rows
        self._columns = columns
        self.flags_calls = 0

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else self._rows

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else self._columns

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        return "x" if role == Qt.ItemDataRole.DisplayRole else None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        self.flags_calls += 1
        return super().flags(index)


def test_header_repaint_with_all_rows_selected_does_not_scan_every_row(qtbot) -> None:
    rows = 2000
    model = _CountingModel(rows, 4)
    table = MultiColumnSortTable()
    qtbot.addWidget(table)
    table.setSourceModel(model)
    table.resize(600, 300)

    table.selectAll()
    model.flags_calls = 0
    # grab() paints even when the offscreen window is never exposed.
    table.horizontalHeader().grab()

    assert model.flags_calls < rows
