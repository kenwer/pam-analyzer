"""Popup editors that write canonical filter text into a filter slot.

Each popup is a plain QWidget exposing ``applyRequested(str)`` with the
canonical text for its operator, so the widgets are unit-testable without
exec'ing a menu. show_filter_popup wraps one in a QMenu + QWidgetAction
(the same idiom as the examine panel's padding popup) anchored under the
funnel button. The typed filter text stays the single source of truth:
popups only prefill from it and write back to it.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QDate, QPoint, Qt, QTime, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDateEdit,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from ..domain.filter_ops import (
    FilterOp,
    parse_date,
    parse_date_range,
    parse_set_values,
    parse_time_range,
)

POPUP_OPS: frozenset[FilterOp] = frozenset(
    {
        FilterOp.ON_DATE,
        FilterOp.BEFORE_DATE,
        FilterOp.AFTER_DATE,
        FilterOp.DATE_RANGE,
        FilterOp.TIME_OF_DAY_RANGE,
        FilterOp.IS_ANY_OF,
    }
)
"""Ops whose selection in the funnel menu opens an editor popup."""

_DATE_FORMAT = "yyyy-MM-dd"
_TIME_FORMAT = "HH:mm"


def _qdate(text: str) -> QDate:
    d = parse_date(text)
    return QDate(d.year, d.month, d.day) if d else QDate.currentDate()


def _add_apply(popup: QWidget, layout: QVBoxLayout | QFormLayout) -> QPushButton:
    """Append an Apply button and bind Return/Enter to it.

    QPushButton.setDefault does not fire inside popup menus, so an explicit
    QShortcut carries the Return key instead.
    """
    button = QPushButton("Apply", popup)
    layout.addRow("", button) if isinstance(layout, QFormLayout) else layout.addWidget(button)
    QShortcut(QKeySequence(Qt.Key.Key_Return), popup, activated=button.click)
    QShortcut(QKeySequence(Qt.Key.Key_Enter), popup, activated=button.click)
    return button


class SingleDatePopup(QWidget):
    """Editor for ON_DATE / BEFORE_DATE / AFTER_DATE. Emits "yyyy-MM-dd"."""

    applyRequested = Signal(str)

    def __init__(self, current_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._date = QDateEdit(self)
        self._date.setCalendarPopup(True)
        self._date.setDisplayFormat(_DATE_FORMAT)
        self._date.setDate(_qdate(current_text))

        layout = QFormLayout(self)
        layout.addRow("Date:", self._date)
        _add_apply(self, layout).clicked.connect(self._apply)

    def _apply(self) -> None:
        self.applyRequested.emit(self._date.date().toString(_DATE_FORMAT))


class DateRangePopup(QWidget):
    """Editor for DATE_RANGE. Emits "yyyy-MM-dd .. yyyy-MM-dd"."""

    applyRequested = Signal(str)

    def __init__(self, current_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._from = QDateEdit(self)
        self._to = QDateEdit(self)
        for edit in (self._from, self._to):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat(_DATE_FORMAT)
        rng = parse_date_range(current_text)
        if rng:
            lo, hi = rng
            self._from.setDate(QDate(lo.year, lo.month, lo.day))
            self._to.setDate(QDate(hi.year, hi.month, hi.day))
        else:
            self._from.setDate(QDate.currentDate())
            self._to.setDate(QDate.currentDate())

        layout = QFormLayout(self)
        layout.addRow("From:", self._from)
        layout.addRow("To:", self._to)
        _add_apply(self, layout).clicked.connect(self._apply)

    def _apply(self) -> None:
        lo = self._from.date().toString(_DATE_FORMAT)
        hi = self._to.date().toString(_DATE_FORMAT)
        self.applyRequested.emit(f"{lo} .. {hi}")


class TimeRangePopup(QWidget):
    """Editor for TIME_OF_DAY_RANGE. Emits "HH:mm - HH:mm"."""

    applyRequested = Signal(str)

    def __init__(self, current_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._from = QTimeEdit(self)
        self._to = QTimeEdit(self)
        for edit in (self._from, self._to):
            edit.setDisplayFormat(_TIME_FORMAT)
        rng = parse_time_range(current_text)
        if rng:
            lo, hi = rng
            self._from.setTime(QTime(lo.hour, lo.minute))
            self._to.setTime(QTime(hi.hour, hi.minute))
        else:
            self._from.setTime(QTime(0, 0))
            self._to.setTime(QTime(23, 59))

        hint = QLabel("End before start = overnight range", self)
        hint.setStyleSheet("color: #777; font-size: 11px;")

        layout = QFormLayout(self)
        layout.addRow("From:", self._from)
        layout.addRow("To:", self._to)
        layout.addRow("", hint)
        _add_apply(self, layout).clicked.connect(self._apply)

    def _apply(self) -> None:
        lo = self._from.time().toString(_TIME_FORMAT)
        hi = self._to.time().toString(_TIME_FORMAT)
        self.applyRequested.emit(f"{lo} - {hi}")


class SetPopup(QWidget):
    """Checkbox list of distinct values for IS_ANY_OF. Emits "a; b; c".

    A value that itself contains ";" cannot round-trip through the text
    grammar; the categorical columns hold ARU/species/model names where
    that does not occur.
    """

    applyRequested = Signal(str)

    def __init__(
        self,
        values: list[str],
        current_text: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        selected = {v.casefold() for v in parse_set_values(current_text)}

        self._search = QLineEdit(self)
        self._search.setPlaceholderText("Search...")
        self._search.textChanged.connect(self._apply_search)

        self._list = QListWidget(self)
        self._list.setMaximumHeight(300)
        for value in values:
            item = QListWidgetItem(value)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = value.casefold() in selected
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self._list.addItem(item)

        all_button = QPushButton("All", self)
        none_button = QPushButton("None", self)
        all_button.clicked.connect(lambda: self._set_visible_checked(True))
        none_button.clicked.connect(lambda: self._set_visible_checked(False))
        buttons = QHBoxLayout()
        buttons.addWidget(all_button)
        buttons.addWidget(none_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self._search)
        layout.addWidget(self._list)
        layout.addLayout(buttons)
        _add_apply(self, layout).clicked.connect(self._apply)

    def _items(self) -> list[QListWidgetItem]:
        return [self._list.item(i) for i in range(self._list.count())]

    def _apply_search(self, text: str) -> None:
        needle = text.strip().casefold()
        for item in self._items():
            item.setHidden(bool(needle) and needle not in item.text().casefold())

    def _set_visible_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for item in self._items():
            if not item.isHidden():
                item.setCheckState(state)

    def checked_values(self) -> list[str]:
        return [i.text() for i in self._items() if i.checkState() is Qt.CheckState.Checked]

    def _apply(self) -> None:
        self.applyRequested.emit("; ".join(self.checked_values()))


def create_popup(
    op: FilterOp,
    current_text: str,
    values_provider: Callable[[], list[str]] | None = None,
) -> QWidget | None:
    """Build the editor popup for *op*, prefilled from *current_text*.

    Returns None for ops without a popup editor.
    """
    if op in (FilterOp.ON_DATE, FilterOp.BEFORE_DATE, FilterOp.AFTER_DATE):
        return SingleDatePopup(current_text)
    if op is FilterOp.DATE_RANGE:
        return DateRangePopup(current_text)
    if op is FilterOp.TIME_OF_DAY_RANGE:
        return TimeRangePopup(current_text)
    if op is FilterOp.IS_ANY_OF:
        values = values_provider() if values_provider is not None else []
        return SetPopup(values, current_text)
    return None


def show_filter_popup(
    widget: QWidget,
    anchor: QWidget,
    on_apply: Callable[[str], None],
) -> None:
    """Show *widget* in a popup menu anchored under *anchor*.

    Apply forwards the canonical text to *on_apply* and closes the popup;
    dismissing the popup any other way changes nothing.
    """
    menu = QMenu(anchor)
    action = QWidgetAction(menu)
    action.setDefaultWidget(widget)
    menu.addAction(action)

    def _apply(text: str) -> None:
        on_apply(text)
        menu.close()

    widget.applyRequested.connect(_apply)
    menu.exec(anchor.mapToGlobal(QPoint(0, anchor.height())))
