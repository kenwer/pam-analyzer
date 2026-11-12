"""Modeless dialog listing the keyboard shortcuts from README.md for the running platform."""

import re
import sys

from PySide6.QtGui import QTextDocument, QTextLength, QTextTable
from PySide6.QtWidgets import QDialog, QWidget

from ..qresource import read_qresource_text
from .ui_shortcuts_dialog import Ui_ShortcutsDialog

# Applied to every table so their columns line up, since QTextDocument
# otherwise sizes each markdown table's columns independently.
_COLUMN_WIDTH_PERCENTAGES = [20, 26, 54]

# The position of each platform's key column in the README's shortcut tables, Linux's for any other platform
_KEY_COLUMNS = {"win32": 0, "darwin": 2}
_LINUX_KEY_COLUMN = 1
_KEY_COLUMN_COUNT = 3


class ShortcutsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.ui = Ui_ShortcutsDialog()
        self.ui.setupUi(self)

        # README.md is the single source for the shortcuts, bundled so release builds pick up its current text
        readme = read_qresource_text(":/docs/README.md")
        self.ui.shortcuts_text_browser.setMarkdown(shortcuts_for_platform(shortcut_tables(readme)))
        _align_document_tables(self.ui.shortcuts_text_browser.document(), _COLUMN_WIDTH_PERCENTAGES)


def shortcut_tables(readme: str) -> str:
    """The README's keyboard shortcuts section without its intro, which points to this dialog.

    The table headings are level 3 to nest under the README's section, but Qt renders those
    barely larger than body text, so they are raised to level 2.
    """
    _, found, rest = readme.partition("\n## Keyboard shortcuts\n")
    if not found:
        return ""
    section = re.split(r"\n#{1,2} ", rest, maxsplit=1)[0].strip()
    first_table_heading = section.find("### ")
    tables = section[first_table_heading:] if first_table_heading >= 0 else section
    return re.sub(r"^### ", "## ", tables, flags=re.MULTILINE)


def shortcuts_for_platform(markdown: str, platform: str = sys.platform) -> str:
    """The shortcut tables with only the platform's key column."""
    key_column = _KEY_COLUMNS.get(platform, _LINUX_KEY_COLUMN)
    lines = []
    for line in markdown.splitlines():
        if line.startswith("|"):
            cells = [cell.strip() for cell in line.split("|")[1:-1]]
            line = "| " + " | ".join([cells[key_column], *cells[_KEY_COLUMN_COUNT:]]) + " |"
        lines.append(line)
    return "\n".join(lines)


def _align_document_tables(document: QTextDocument, column_width_percentages: list[int]) -> None:
    constraints = [QTextLength(QTextLength.Type.PercentageLength, pct) for pct in column_width_percentages]
    for frame in document.rootFrame().childFrames():
        if isinstance(frame, QTextTable):
            table_format = frame.format()
            table_format.setColumnWidthConstraints(constraints)
            frame.setFormat(table_format)
