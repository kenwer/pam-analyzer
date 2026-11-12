"""ShortcutsDialog renders the README's keyboard shortcuts section for the running platform."""

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QTextTable

from pam_analyzer.ui.dialogs.shortcuts_dialog import ShortcutsDialog, shortcut_tables, shortcuts_for_platform

_README = """# App

## Keyboard shortcuts
The shortcuts are also listed in the app's menu.

### Global shortcuts
| Windows | Linux | macOS | Action | Description |
| --- | --- | --- | --- | --- |
| Ctrl+Q | Ctrl+Shift+Q | ⌘Q | **Quit** | Exit |

## Core Concepts
Not a shortcut.
"""


def _tables(dialog: ShortcutsDialog) -> list[QTextTable]:
    frames = dialog.ui.shortcuts_text_browser.document().rootFrame().childFrames()
    return [frame for frame in frames if isinstance(frame, QTextTable)]


def test_shortcut_tables_drops_intro_and_following_sections():
    tables = shortcut_tables(_README)
    assert tables.startswith("## Global shortcuts\n")
    assert "menu" not in tables
    assert "Core Concepts" not in tables


@pytest.mark.parametrize(
    ("platform", "header", "key"),
    [
        ("win32", "Windows", "Ctrl+Q"),
        ("linux", "Linux", "Ctrl+Shift+Q"),
        ("darwin", "macOS", "⌘Q"),
        ("freebsd14", "Linux", "Ctrl+Shift+Q"),
    ],
)
def test_shortcuts_for_platform_keeps_only_its_key_column(platform, header, key):
    markdown = shortcuts_for_platform(shortcut_tables(_README), platform)
    assert markdown.splitlines()[1:] == [
        f"| {header} | Action | Description |",
        "| --- | --- | --- |",
        f"| {key} | **Quit** | Exit |",
    ]


def test_renders_shortcuts_from_bundled_readme(qtbot):
    dialog = ShortcutsDialog()
    qtbot.addWidget(dialog)
    text = dialog.ui.shortcuts_text_browser.toPlainText()
    assert "Open Project Folder" in text
    assert "Corrected Species" in text
    assert "Core Concepts" not in text


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
    assert len(widths[0]) == tables[0].columns() == 3
