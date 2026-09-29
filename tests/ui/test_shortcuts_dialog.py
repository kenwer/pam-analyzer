"""ShortcutsDialog renders SHORTCUTS.md from the Qt resources."""

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QTextTable

from pam_analyzer.ui.dialogs.shortcuts_dialog import ShortcutsDialog


def _tables(dialog: ShortcutsDialog) -> list[QTextTable]:
    frames = dialog.ui.shortcuts_text_browser.document().rootFrame().childFrames()
    return [frame for frame in frames if isinstance(frame, QTextTable)]


def test_renders_shortcuts_from_resource(qtbot):
    dialog = ShortcutsDialog()
    qtbot.addWidget(dialog)
    text = dialog.ui.shortcuts_text_browser.toPlainText()
    assert "Open Project Folder" in text
    assert "Corrected Species" in text


def test_can_be_reshown_after_close(qtbot):
    # MainWindow keeps one instance and reshows it from the Help menu.
    dialog = ShortcutsDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.close()
    # Run any deleteLater() the close scheduled, which processEvents() skips.
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    dialog.show()
    assert dialog.isVisible()


def test_all_tables_share_column_widths(qtbot):
    dialog = ShortcutsDialog()
    qtbot.addWidget(dialog)
    tables = _tables(dialog)
    assert len(tables) >= 3
    widths = [table.format().columnWidthConstraints() for table in tables]
    assert all(w == widths[0] for w in widths)
    assert len(widths[0]) == tables[0].columns()
