"""Welcome panel: the loading lock and dropping a project folder onto it."""

from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent

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
        QPoint(10, 10), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    )
    panel.dragEnterEvent(event)
    return event.isAccepted()


def _drop(panel: WelcomePanel, mime: QMimeData) -> None:
    event = QDropEvent(
        QPoint(10, 10), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
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
