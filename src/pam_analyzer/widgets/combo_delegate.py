"""QStyledItemDelegate that pops up a fixed-value QComboBox for editing.

Mirrors AG Grid's ``agSelectCellEditor`` pattern: the cell renders the
plain string value, but double-clicking (or pressing F2) opens a
combobox limited to a known set of choices.
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QModelIndex, Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)


class ComboDelegate(QStyledItemDelegate):
    """Item delegate that edits with a QComboBox of fixed values.

    The choice list is read from a callable so callers can update it
    without rebuilding the delegate (e.g. as new species appear after a
    detection load).
    """

    def __init__(
        self,
        choices_provider,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._choices_provider = choices_provider

    def setChoicesProvider(self, provider) -> None:  # noqa: N802 (Qt-style)
        self._choices_provider = provider

    def createEditor(
        self,
        parent: QWidget,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> QWidget:  # noqa: N802 (Qt API)
        del option, index
        combo = QComboBox(parent)
        combo.setEditable(False)
        combo.addItems(list(self._choices_provider() or ()))

        # Open the popup once the editor is actually on-screen. Calling
        # showPopup() directly inside createEditor is unreliable because the
        # combo isn't reparented or visible yet at that point.
        QTimer.singleShot(0, combo.showPopup)

        # Commit-and-close as soon as the user picks a value. Use `activated`
        # rather than `currentIndexChanged` so the editor doesn't close itself
        # during setEditorData's programmatic selection.
        combo.activated.connect(lambda _i: self._commit_and_close(combo))
        return combo

    def _commit_and_close(self, editor: QComboBox) -> None:
        self.commitData.emit(editor)
        self.closeEditor.emit(editor)

    def setEditorData(self, editor: QWidget, index: QModelIndex) -> None:  # noqa: N802 (Qt API)
        if not isinstance(editor, QComboBox):
            return
        value = "" if index.data(Qt.EditRole) is None else str(index.data(Qt.EditRole))
        pos = editor.findText(value)
        editor.setCurrentIndex(pos if pos >= 0 else 0)

    def setModelData(  # noqa: N802 (Qt API)
        self, editor: QWidget, model, index: QModelIndex
    ) -> None:
        if not isinstance(editor, QComboBox):
            return
        model.setData(index, editor.currentText(), Qt.EditRole)

    def updateEditorGeometry(  # noqa: N802 (Qt API)
        self, editor: QWidget, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        editor.setGeometry(option.rect)


def fixed(values: Iterable[str]):
    """Return a choices_provider callable that always yields *values*."""
    items = tuple(values)
    return lambda: items


__all__ = ["ComboDelegate", "fixed"]
