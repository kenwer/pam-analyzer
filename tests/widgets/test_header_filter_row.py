from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel

from pam_analyzer.widgets.header_filter_row import HeaderFilterRow
from pam_analyzer.widgets.multi_column_sort_table import MultiColumnSortTable


def test_filter_row_follows_columns_resized_to_contents(qtbot) -> None:
    model = QStandardItemModel(3, 4)
    model.setHorizontalHeaderLabels(["A", "B", "C", "D"])
    for row in range(3):
        for col in range(4):
            model.setItem(row, col, QStandardItem("x"))
    table = MultiColumnSortTable()
    qtbot.addWidget(table)
    table.setSourceModel(model)
    filter_row = HeaderFilterRow(table)
    filter_row.rebuild(model.columnCount())
    table.resize(800, 300)
    table.show()
    # Drain the deferred sync from the show event so it can't mask a stale layout.
    qtbot.wait(10)
    table.resizeColumnsToContents()

    model.setHeaderData(1, Qt.Horizontal, "B with a much longer header")
    table.resizeColumnsToContents()

    header = table.horizontalHeader()

    def aligned() -> bool:
        return all(
            (slot.edit.x(), slot.edit.width()) == (header.sectionViewportPosition(col), header.sectionSize(col))
            for col, slot in enumerate(filter_row._slots)
        )

    qtbot.waitUntil(aligned)
