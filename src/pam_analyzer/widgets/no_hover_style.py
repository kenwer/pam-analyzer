"""Item-view proxy style that suppresses the per-cell hover highlight.

Windows' QWindowsVistaStyle sets State_MouseOver on every item under the
cursor, producing a cell highlight on top of the row-selection background.
Clearing the flag here before delegating to the base style suppresses that
highlight across all delegates without modifying any of them.
"""

from __future__ import annotations

from PySide6.QtWidgets import QAbstractItemView, QProxyStyle, QStyle, QStyleOptionViewItem


class NoHoverProxyStyle(QProxyStyle):
    def drawControl(self, element, option, painter, widget=None) -> None:  # noqa: N802 (Qt API)
        if element == QStyle.ControlElement.CE_ItemViewItem:
            opt = QStyleOptionViewItem(option)
            opt.state &= ~QStyle.StateFlag.State_MouseOver
            super().drawControl(element, opt, painter, widget)
            return
        super().drawControl(element, option, painter, widget)


def disable_item_hover(view: QAbstractItemView) -> None:
    """Apply NoHoverProxyStyle to *view* and keep the style alive.

    QWidget.setStyle does not take ownership, so the proxy must be retained
    on the Python side or it will be garbage-collected and crash during paint.
    We construct QProxyStyle with no base argument because passing one causes
    QProxyStyle to adopt and later destroy that style; the empty form forwards
    to the application style without owning it.
    """
    style = NoHoverProxyStyle()
    view.setStyle(style)
    view._no_hover_style = style  # noqa: SLF001 (intentional retention)
