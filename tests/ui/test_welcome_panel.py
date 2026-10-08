"""Welcome panel: the loading lock and dropping a project folder onto it."""

from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QPointF, Qt, QTimer, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication

from pam_analyzer.ui.panels.welcome_panel import WelcomePanel


@pytest.fixture
def panel(qtbot) -> WelcomePanel:
    p = WelcomePanel()
    qtbot.addWidget(p)
    return p


def _mime(*paths: Path) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return mime


def _drag_enter(panel: WelcomePanel, mime: QMimeData) -> bool:
    event = QDragEnterEvent(
        QPointF(10, 10), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    )
    panel.dragEnterEvent(event)
    return event.isAccepted()


def _drop(panel: WelcomePanel, mime: QMimeData) -> None:
    event = QDropEvent(
        QPointF(10, 10), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    )
    panel.dropEvent(event)


def test_loading_locks_actions_and_restores_cursor(panel: WelcomePanel):
    panel.set_loading("proj")
    assert not panel.ui.new_button.isEnabled()
    assert not panel.ui.recent_list.isEnabled()
    assert panel.ui.new_button.cursor().shape() == Qt.CursorShape.ArrowCursor

    panel.clear_loading()
    assert panel.ui.open_project_folder_button.isEnabled()
    assert panel.ui.new_button.cursor().shape() == Qt.CursorShape.PointingHandCursor


def test_drop_single_folder_emits_path(qtbot, panel: WelcomePanel, tmp_path: Path):
    mime = _mime(tmp_path)
    assert _drag_enter(panel, mime)
    assert tmp_path.name in panel.ui.tagline_label.text()

    with qtbot.waitSignal(panel.folderDropped) as blocker:
        _drop(panel, mime)
    assert blocker.args == [str(tmp_path)]
    assert panel.ui.tagline_label.text() == panel._default_tagline


def test_drag_rejects_files_and_multiple_folders(panel: WelcomePanel, tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    f = tmp_path / "rec.wav"
    f.touch()
    assert not _drag_enter(panel, _mime(f))
    assert not _drag_enter(panel, _mime(a, b))


def test_drop_ignored_while_loading(qtbot, panel: WelcomePanel, tmp_path: Path):
    panel.set_loading("other")
    mime = _mime(tmp_path)
    assert not _drag_enter(panel, mime)
    with qtbot.assertNotEmitted(panel.folderDropped):
        _drop(panel, mime)


def _choose_from_context_menu(panel: WelcomePanel, row: int, label: str) -> list[str]:
    """Open the recent list's context menu on *row*, trigger *label*, and return the menu's labels."""
    recent_list = panel.ui.recent_list
    labels: list[str] = []

    def choose() -> None:
        popup = QApplication.activePopupWidget()
        if popup is None:
            return
        labels.extend(a.text() for a in popup.actions())
        for action in popup.actions():
            if action.text() == label:
                action.trigger()
        popup.close()

    QTimer.singleShot(0, choose)
    panel._on_recent_context_menu(recent_list.visualItemRect(recent_list.item(row)).center())
    return labels


def test_context_menu_removes_the_clicked_recent_project(qtbot, panel: WelcomePanel):
    panel.set_recent_projects(["/a/one", "/b/two"])
    with qtbot.waitSignal(panel.removeRecentRequested) as removed:
        labels = _choose_from_context_menu(panel, 1, "Remove from list")

    assert labels == ["Locate…", "Remove from list"]
    assert removed.args == ["/b/two"]


def test_context_menu_locates_the_clicked_recent_project(qtbot, panel: WelcomePanel):
    panel.set_recent_projects(["/a/one", "/b/two"])
    with qtbot.waitSignal(panel.locateRecentRequested) as located:
        _choose_from_context_menu(panel, 0, "Locate…")
    assert located.args == ["/a/one"]


def test_context_menu_on_placeholder_does_nothing(qtbot, panel: WelcomePanel):
    panel.set_recent_projects([])
    with qtbot.assertNotEmitted(panel.removeRecentRequested):
        labels = _choose_from_context_menu(panel, 0, "Remove from list")
    assert labels == []


def test_delete_key_removes_the_current_recent_project(qtbot, panel: WelcomePanel):
    panel.set_recent_projects(["/a/one", "/b/two"])
    recent_list = panel.ui.recent_list
    with qtbot.waitExposed(panel):
        panel.show()
    panel.activateWindow()
    recent_list.setFocus()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is recent_list)
    recent_list.setCurrentRow(0)

    with qtbot.waitSignal(panel.removeRecentRequested) as removed:
        qtbot.keyClick(recent_list, Qt.Key.Key_Delete)
    assert removed.args == ["/a/one"]
