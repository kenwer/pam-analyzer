import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QWidget

from pam_analyzer.domain.filter_ops import FilterOp
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
            (slot.x(), slot.width()) == (header.sectionViewportPosition(col), header.sectionSize(col))
            for col, slot in enumerate(filter_row._slots)
        )

    qtbot.waitUntil(aligned)


@pytest.fixture
def shown_row(qtbot) -> HeaderFilterRow:
    model = QStandardItemModel(3, 2)
    model.setHorizontalHeaderLabels(["Species", "Comment"])
    table = MultiColumnSortTable()
    qtbot.addWidget(table)
    table.setSourceModel(model)
    filter_row = HeaderFilterRow(table)
    filter_row.rebuild(model.columnCount())
    table.resize(600, 200)
    table.show()
    qtbot.waitExposed(table)
    # As in the app, the table holds focus, not the first filter input.
    table.setFocus()
    qtbot.waitUntil(lambda: not filter_row._slots[0].edit.hasFocus())
    # The cursor stays where an earlier test left it, possibly over a cell.
    _hover_away(qtbot, filter_row)
    return filter_row


def _record(filter_row: HeaderFilterRow) -> list[tuple[int, str, FilterOp]]:
    emitted: list[tuple[int, str, FilterOp]] = []
    filter_row.filterChanged.connect(lambda col, text, op: emitted.append((col, text, op)))
    return emitted


def _hover(qtbot, widget: QWidget) -> None:
    qtbot.mouseMove(widget, widget.rect().center())


def _hover_away(qtbot, filter_row: HeaderFilterRow) -> None:
    # The table body, below the header and its filter row.
    table = filter_row._table
    qtbot.mouseMove(table, QPoint(table.width() // 2, table.height() - 10))


def test_clear_button_shows_on_hover_while_the_filter_is_active(qtbot, shown_row: HeaderFilterRow) -> None:
    shown_row.set_filter(0, "robin", FilterOp.CONTAINS)
    slot = shown_row._slots[0]
    assert slot.clear.isHidden()

    _hover(qtbot, slot.edit)
    qtbot.waitUntil(lambda: not slot.clear.isHidden())
    assert slot.clear.geometry().right() < slot.button.geometry().left()

    _hover_away(qtbot, shown_row)
    qtbot.waitUntil(slot.clear.isHidden)


def test_hovering_an_unfiltered_cell_shows_no_clear_button(qtbot, shown_row: HeaderFilterRow) -> None:
    slot = shown_row._slots[1]
    _hover(qtbot, slot.edit)
    qtbot.waitUntil(slot.underMouse)
    assert slot.clear.isHidden()


def test_moving_from_the_input_onto_the_clear_button_keeps_it(qtbot, shown_row: HeaderFilterRow) -> None:
    shown_row.set_filter(0, "robin", FilterOp.CONTAINS)
    slot = shown_row._slots[0]
    _hover(qtbot, slot.edit)
    qtbot.waitUntil(lambda: not slot.clear.isHidden())

    _hover(qtbot, slot.clear)
    qtbot.waitUntil(slot.clear.underMouse)

    assert not slot.clear.isHidden()


def test_clear_button_shows_while_the_input_has_focus(qtbot, shown_row: HeaderFilterRow) -> None:
    shown_row.set_filter(0, "robin", FilterOp.CONTAINS)
    slot = shown_row._slots[0]

    slot.edit.setFocus()
    qtbot.waitUntil(lambda: not slot.clear.isHidden())

    shown_row._table.setFocus()
    qtbot.waitUntil(slot.clear.isHidden)


def test_clear_button_resets_text_and_operator_and_applies_at_once(shown_row: HeaderFilterRow) -> None:
    shown_row.set_filter(0, "robin", FilterOp.EQUALS)
    emitted = _record(shown_row)
    slot = shown_row._slots[0]

    slot.clear.click()

    assert emitted == [(0, "", FilterOp.CONTAINS)]
    assert slot.edit.text() == ""
    assert shown_row.column_op(0) is FilterOp.CONTAINS
    assert slot.clear.isHidden()


def test_clear_button_lifts_a_blank_filter_that_disabled_the_input(shown_row: HeaderFilterRow) -> None:
    shown_row.set_filter(1, "", FilterOp.BLANK)
    slot = shown_row._slots[1]
    assert not slot.edit.isEnabled()

    slot.clear.click()

    assert slot.edit.isEnabled()
    assert shown_row.column_op(1) is FilterOp.CONTAINS


def test_escape_in_the_input_clears_its_filter(qtbot, shown_row: HeaderFilterRow) -> None:
    shown_row.set_filter(0, "robin", FilterOp.CONTAINS)
    emitted = _record(shown_row)
    slot = shown_row._slots[0]

    qtbot.keyClick(slot.edit, Qt.Key.Key_Escape)

    assert emitted == [(0, "", FilterOp.CONTAINS)]
    assert slot.edit.text() == ""
    assert slot.clear.isHidden()


def test_escape_drops_text_still_waiting_for_the_debounce(qtbot, shown_row: HeaderFilterRow) -> None:
    emitted = _record(shown_row)
    slot = shown_row._slots[0]
    qtbot.keyClicks(slot.edit, "rob")

    qtbot.keyClick(slot.edit, Qt.Key.Key_Escape)

    assert slot.edit.text() == ""
    assert not slot.timer.isActive()
    assert emitted == [(0, "", FilterOp.CONTAINS)]
