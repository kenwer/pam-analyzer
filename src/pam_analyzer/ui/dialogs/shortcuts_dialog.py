"""Modeless dialog listing the keyboard shortcuts from SHORTCUTS.md."""

from PySide6.QtGui import QTextDocument, QTextLength, QTextTable
from PySide6.QtWidgets import QDialog, QWidget

from ..qresource import read_qresource_text
from .ui_shortcuts_dialog import Ui_ShortcutsDialog

# Applied to every table so their columns line up, since QTextDocument
# otherwise sizes each markdown table's columns independently.
_COLUMN_WIDTH_PERCENTAGES = [14, 12, 26, 48]


class ShortcutsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.ui = Ui_ShortcutsDialog()
        self.ui.setupUi(self)

        self.ui.shortcuts_text_browser.setMarkdown(read_qresource_text(":/docs/SHORTCUTS.md"))
        _align_document_tables(self.ui.shortcuts_text_browser.document(), _COLUMN_WIDTH_PERCENTAGES)


def _align_document_tables(document: QTextDocument, column_width_percentages: list[int]) -> None:
    constraints = [QTextLength(QTextLength.Type.PercentageLength, pct) for pct in column_width_percentages]
    for frame in document.rootFrame().childFrames():
        if isinstance(frame, QTextTable):
            table_format = frame.format()
            table_format.setColumnWidthConstraints(constraints)
            frame.setFormat(table_format)
