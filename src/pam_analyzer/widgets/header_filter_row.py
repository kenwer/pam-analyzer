"""Per-column filter row embedded in a :class:`MultiColumnSortTable` header.

Provides :class:`HeaderFilterRow`, a QObject controller that places one
filter slot (text input + funnel/operator menu) per model
column inside the header area, with debounced text-change signals.
The set of operators per column depends on the ColumnKind passed for it
at :meth:`rebuild` time.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QPoint, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QActionGroup, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QLineEdit, QMenu, QToolButton, QWidget

from ..domain.filter_ops import (
    DATETIME_OPS,
    ColumnKind,
    FilterOp,
    default_op,
    label_for,
    needs_value,
    operators_for,
)
from .filter_popups import POPUP_OPS, create_popup, show_filter_popup

if TYPE_CHECKING:
    from .multi_column_sort_table import MultiColumnSortTable


# Inset between the funnel button and the QLineEdit's edges. Stays at one
# logical pixel since Qt high-DPI scaling already handles the device pixel
# ratio.
_BUTTON_PAD = 1


class _FunnelButton(QToolButton):
    """Funnel-icon button overlaid on the right of a filter input.

    Painted by hand (rather than via a stylesheet/icon) so the active state
    is obvious at a glance: outline-only when no filter is active, filled
    blue when one is. The funnel polygon is computed as fractions of the
    widget's current size, so the icon scales with whatever square dimension
    the parent picks based on font metrics.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAutoRaise(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._active = False

    def setActive(self, active: bool) -> None:  # noqa: N802 (Qt-style)
        if self._active == active:
            return
        self._active = active
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Qt-required signature. We always repaint the whole widget.
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self.underMouse():
            # Hover background. Matches a faint header section highlight.
            painter.fillRect(self.rect(), self.palette().midlight())

        # Funnel polygon: trapezoid on top tapering into a narrow stem.
        # Coordinates are fractions of the widget size so the icon adapts
        # to whatever font-driven size the parent assigns.
        w, h = self.width(), self.height()
        cx = w / 2
        top_y = h * 0.20
        mid_y = h * 0.50
        bot_y = h * 0.82
        half_top = w * 0.32
        half_mid = max(1.0, w * 0.08)
        funnel = QPolygonF(
            [
                QPointF(cx - half_top, top_y),
                QPointF(cx + half_top, top_y),
                QPointF(cx + half_mid, mid_y),
                QPointF(cx + half_mid, bot_y),
                QPointF(cx - half_mid, bot_y),
                QPointF(cx - half_mid, mid_y),
            ]
        )

        if self._active:
            painter.setPen(QPen(self.palette().highlight().color().darker(140), 1))
            painter.setBrush(self.palette().highlight())
        else:
            painter.setPen(QPen(self.palette().mid().color(), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolygon(funnel)


@dataclass
class _Slot:
    edit: QLineEdit
    button: _FunnelButton
    timer: QTimer
    kind: ColumnKind
    op: FilterOp
    pending_text: str = ""


class HeaderFilterRow(QObject):
    """A controller that places one filter slot per model column inside the
    header area, aligned with the header sections.

    Communicates filter changes via ``filterChanged(col, text, op)`` where
    *col* is the logical model column index and *op* is a :class:`FilterOp`.
    """

    filterChanged = Signal(int, str, object)

    def __init__(self, table: MultiColumnSortTable) -> None:
        super().__init__(table)
        self._table = table
        self._header = table.horizontalHeader()
        self._slots: list[_Slot] = []
        self._suppressed: set[int] = set()
        # Distinct-values source for the "is one of" popup, injected by the
        # owning table so this controller stays decoupled from the model.
        self._values_provider: Callable[[int], list[str]] | None = None

        # Drive height + button size from a sample QLineEdit's sizeHint, which
        # already accounts for the user's font, style, and high-DPI scaling.
        self._height = QLineEdit().sizeHint().height()
        # Square funnel button comfortably inset inside the line edit. Floor
        # at 12 logical px so the icon stays legible on tiny fonts.
        self._button_size = max(12, self._height - 2 * (_BUTTON_PAD + 1))
        self._header.setFilterHeight(self._height)

        self._header.sectionResized.connect(self._sync)
        self._header.sectionMoved.connect(self._sync)
        self._header.geometriesChanged.connect(self._sync)
        table.horizontalScrollBar().valueChanged.connect(self._sync)
        table.verticalScrollBar().valueChanged.connect(self._sync)
        self._header.shown.connect(lambda: QTimer.singleShot(0, self._sync))

    # public API
    def rebuild(self, col_count: int, kinds: Mapping[int, ColumnKind] | None = None) -> None:
        """Recreate one filter slot per model column.

        *kinds* maps column index to its ColumnKind, which picks the
        operator menu for that slot. Missing entries default to TEXT.
        """
        kinds = kinds or {}
        for s in self._slots:
            s.timer.stop()
            s.edit.deleteLater()
            s.button.deleteLater()
        self._slots = []

        for col in range(col_count):
            self._slots.append(self._build_slot(col, kinds.get(col, ColumnKind.TEXT)))
        self._sync()

    def set_values_provider(self, provider: Callable[[int], list[str]]) -> None:
        """Set the source of distinct column values for the set popup."""
        self._values_provider = provider

    def clear_column(self, col: int) -> None:
        if not (0 <= col < len(self._slots)):
            return
        slot = self._slots[col]
        slot.edit.blockSignals(True)
        slot.edit.clear()
        slot.edit.blockSignals(False)
        slot.pending_text = ""
        slot.button.setActive(False)

    def set_column_visible(self, col: int, visible: bool) -> None:
        if 0 <= col < len(self._slots):
            if visible:
                self._suppressed.discard(col)
            else:
                self.clear_column(col)
                self._suppressed.add(col)
            self._sync()

    def is_filter_visible(self, col: int) -> bool:
        """Return whether the filter input for *col* is currently visible."""
        if not (0 <= col < len(self._slots)):
            return False
        return col not in self._suppressed and self._slots[col].edit.isVisible()

    def column_op(self, col: int) -> FilterOp | None:
        """Return the active operator for *col*, or ``None`` if out of range."""
        if 0 <= col < len(self._slots):
            return self._slots[col].op
        return None

    # slot construction
    def _build_slot(self, col: int, kind: ColumnKind) -> _Slot:
        edit = QLineEdit(self._header)
        edit.setPlaceholderText("…")
        # Reserve room on the right for the funnel button.
        edit.setTextMargins(0, 0, self._button_size + _BUTTON_PAD * 2, 0)
        edit.setStyleSheet(
            "QLineEdit { padding: 1px 3px; "
            "border: 1px solid #ccc; border-radius: 2px; background: white; }"
            "QLineEdit:focus { border-color: #4a90d9; }"
            "QLineEdit:disabled { background: #f3f3f3; color: #888; }"
        )

        button = _FunnelButton(edit)
        op = default_op(kind)
        button.setToolTip(label_for(op))

        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(300)

        slot = _Slot(edit=edit, button=button, timer=timer, kind=kind, op=op)

        def _on_text(text: str, c: int = col) -> None:
            self._slots[c].pending_text = text
            self._slots[c].timer.start()

        def _on_timer(c: int = col) -> None:
            self._emit(c)

        def _on_button() -> None:
            self._show_op_menu(col)

        timer.timeout.connect(_on_timer)
        edit.textChanged.connect(_on_text)
        button.clicked.connect(_on_button)
        return slot

    # operator menu
    def _show_op_menu(self, col: int) -> None:
        slot = self._slots[col]
        menu = QMenu(slot.edit)
        group = QActionGroup(menu)
        group.setExclusive(True)
        ops = operators_for(slot.kind)
        rich_ops = POPUP_OPS | DATETIME_OPS
        for i, op in enumerate(ops):
            # Separator between the popup-editor ops and the plain text ops.
            if i > 0 and op not in rich_ops and ops[i - 1] in rich_ops:
                menu.addSeparator()
            action = menu.addAction(label_for(op))
            action.setCheckable(True)
            action.setChecked(op is slot.op)
            group.addAction(action)
            # `_checked` swallows QAction.triggered's bool arg. We don't use it.
            action.triggered.connect(lambda _checked=False, o=op, c=col: self._set_op(c, o))
        # Anchor the menu under the funnel button.
        global_pos = slot.button.mapToGlobal(QPoint(0, slot.button.height()))
        menu.exec(global_pos)

    def _set_op(self, col: int, op: FilterOp) -> None:
        slot = self._slots[col]
        slot.op = op
        slot.button.setToolTip(label_for(op))
        # BLANK and NOT_BLANK ignore the typed value. Visually disable the input.
        value_required = needs_value(op)
        slot.edit.setEnabled(value_required)
        if not value_required:
            # Clear text but keep the operator-driven filter active.
            slot.edit.blockSignals(True)
            slot.edit.clear()
            slot.edit.blockSignals(False)
            slot.pending_text = ""
        # Re-apply immediately rather than waiting for the next keystroke.
        self._emit(col)
        if op in POPUP_OPS:
            # Re-selecting the checked op also lands here (triggered still
            # fires in an exclusive QActionGroup), which is how users reopen
            # the editor. Deferred so the op menu fully closes first; opening
            # a second popup from inside a closing menu's handler is flaky.
            QTimer.singleShot(0, lambda c=col: self._open_popup(c))

    def _open_popup(self, col: int) -> None:
        """Open the editor popup for the column's current op, prefilled from
        its text. Apply writes canonical text back; dismiss changes nothing."""
        if not (0 <= col < len(self._slots)):
            return
        slot = self._slots[col]
        values_provider = None
        if self._values_provider is not None:
            provider = self._values_provider
            values_provider = lambda c=col: provider(c)  # noqa: E731
        widget = create_popup(slot.op, slot.edit.text(), values_provider)
        if widget is None:
            return

        def _apply(text: str, c: int = col) -> None:
            s = self._slots[c]
            s.timer.stop()
            s.edit.blockSignals(True)
            s.edit.setText(text)
            s.edit.blockSignals(False)
            s.pending_text = text
            self._emit(c)

        show_filter_popup(widget, slot.button, _apply)

    # emit
    def _emit(self, col: int) -> None:
        slot = self._slots[col]
        text = slot.pending_text if needs_value(slot.op) else ""
        active = (needs_value(slot.op) and bool(text.strip())) or not needs_value(slot.op)
        slot.button.setActive(active)
        self.filterChanged.emit(col, text, slot.op)

    # layout
    def _sync(self, *_: object) -> None:
        """Reposition all slots to match the current header geometry."""
        header = self._header
        y = header.height() - self._height
        size = self._button_size
        for col, slot in enumerate(self._slots):
            if col in self._suppressed or self._table.isColumnHidden(col):
                slot.edit.hide()
                continue
            x = header.sectionViewportPosition(col)
            w = header.sectionSize(col)
            slot.edit.setGeometry(x, y, w, self._height)
            slot.edit.show()
            # Position the funnel inside the QLineEdit, hugging the right edge.
            btn_x = max(0, w - size - _BUTTON_PAD)
            btn_y = (self._height - size) // 2
            slot.button.setGeometry(btn_x, btn_y, size, size)
            slot.button.show()
