"""Welcome screen shown when no project is loaded.

Offers buttons to create / open a project plus a list of recent projects.
An entry's context menu relocates or removes it, and Delete removes the
selected one. The panel also accepts a project folder dropped from the file
manager. The panel itself
is stateless about persistence, it emits signals. The main window owns
AppSettings and the open/create handlers.
"""

from pathlib import Path

from PySide6.QtCore import QMimeData, QModelIndex, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QFont,
    QFontMetrics,
    QIcon,
    QKeySequence,
    QPainter,
    QPalette,
    QShortcut,
)
from PySide6.QtWidgets import (
    QListWidgetItem,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from ...domain import paths
from .. import resources_rc  # noqa: F401  registers :/icons/* resources
from .ui_welcome_panel import Ui_WelcomePanel


class WelcomePanel(QWidget):
    newRequested = Signal()
    openProjectFolderRequested = Signal()
    recentRequested = Signal(str)  # path str
    removeRecentRequested = Signal(str)  # path str
    locateRecentRequested = Signal(str)  # path str
    folderDropped = Signal(str)  # path str

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ui = Ui_WelcomePanel()
        self.ui.setupUi(self)

        self.ui.icon_label.setPixmap(QIcon(":/icons/icon.svg").pixmap(QSize(112, 112)))
        self.ui.recent_list.setItemDelegate(_RecentProjectDelegate(self.ui.recent_list))
        self._default_tagline = self.ui.tagline_label.text()
        self._default_tagline_style = self.ui.tagline_label.styleSheet()
        self._loading = False
        self.setAcceptDrops(True)

        self.ui.new_button.clicked.connect(self.newRequested.emit)
        self.ui.open_project_folder_button.clicked.connect(self.openProjectFolderRequested.emit)
        self.ui.recent_list.itemActivated.connect(self._on_recent_activated)
        self.ui.recent_list.itemClicked.connect(self._on_recent_activated)
        self.ui.recent_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.ui.recent_list.customContextMenuRequested.connect(self._on_recent_context_menu)
        remove = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.ui.recent_list)
        remove.setContext(Qt.ShortcutContext.WidgetShortcut)
        remove.activated.connect(lambda: self._remove_recent(self.ui.recent_list.currentItem()))

    def set_loading(self, project_name: str) -> None:
        """Show a loading state in place of the usual tagline while a folder
        opens, and block further open/create/recent actions until it clears."""
        self._show_status(f"Opening {project_name}...")
        self._set_actions_enabled(False)

    def clear_loading(self) -> None:
        self._show_tagline()
        self._set_actions_enabled(True)

    def _show_status(self, text: str) -> None:
        # The idle tagline's placeholder gray is too faint for a status message.
        self.ui.tagline_label.setStyleSheet("color: palette(text); font-weight: 600;")
        self.ui.tagline_label.setText(text)

    def _show_tagline(self) -> None:
        self.ui.tagline_label.setStyleSheet(self._default_tagline_style)
        self.ui.tagline_label.setText(self._default_tagline)

    def _set_actions_enabled(self, enabled: bool) -> None:
        self._loading = not enabled
        # Qt keeps showing a widget's own cursor while it is disabled, so a
        # pointing hand would still suggest the buttons are clickable.
        cursor = Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor
        for button in (self.ui.new_button, self.ui.open_project_folder_button):
            button.setEnabled(enabled)
            button.setCursor(cursor)
        self.ui.recent_list.setEnabled(enabled)

    def set_recent_projects(self, paths: list[str]) -> None:
        self.ui.recent_list.clear()
        if not paths:
            placeholder = QListWidgetItem("No recent projects")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            placeholder.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.ui.recent_list.addItem(placeholder)
            return

        for path_str in paths:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, path_str)
            self.ui.recent_list.addItem(item)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        folder = None if self._loading else _dropped_folder(event.mimeData())
        if folder is None:
            event.ignore()
            return
        self._show_status(f"Drop to open {folder.name}")
        event.acceptProposedAction()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._show_tagline()
        event.accept()

    def dropEvent(self, event: QDropEvent) -> None:
        self._show_tagline()
        folder = None if self._loading else _dropped_folder(event.mimeData())
        if folder is None:
            event.ignore()
            return
        event.acceptProposedAction()
        self.folderDropped.emit(str(folder))

    def _on_recent_activated(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(path, str):
            self.recentRequested.emit(path)

    def _on_recent_context_menu(self, pos: QPoint) -> None:
        item = self.ui.recent_list.itemAt(pos)
        path = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(path, str):
            return
        menu = QMenu(self)
        # Connected to the action rather than read from exec()'s return value,
        # so a programmatic trigger (tests) takes the same path as a click.
        menu.addAction("Locate…").triggered.connect(lambda: self.locateRecentRequested.emit(path))
        menu.addAction("Remove from list").triggered.connect(lambda: self._remove_recent(item))
        menu.exec(self.ui.recent_list.viewport().mapToGlobal(pos))

    def _remove_recent(self, item: QListWidgetItem | None) -> None:
        path = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if isinstance(path, str):
            self.removeRecentRequested.emit(path)


def _dropped_folder(mime: QMimeData) -> Path | None:
    """The folder a drag carries, or None unless it is exactly one local directory."""
    urls = mime.urls() if mime.hasUrls() else []
    if len(urls) != 1 or not urls[0].isLocalFile():
        return None
    folder = Path(urls[0].toLocalFile())
    return folder if folder.is_dir() else None


class _RecentProjectDelegate(QStyledItemDelegate):
    """Paints each recent-project row as a centered bold name over a gray path.

    Painting directly into the row's rect (rather than a QListWidget item
    widget) means the two lines stay centered across the full row width
    automatically as the list is resized, no manual widget sizing needed.
    """

    _MARGIN = 6
    _SPACING = 2

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._name_font = QFont()
        self._name_font.setBold(True)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        path_str = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(path_str, str):
            super().paint(painter, option, index)
            return

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        opt.widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)

        name_metrics = QFontMetrics(self._name_font)
        path_metrics = QFontMetrics(option.font)
        rect = option.rect

        y = rect.top() + self._MARGIN
        name_rect = QRect(rect.left(), y, rect.width(), name_metrics.height())
        y += name_metrics.height() + self._SPACING
        path_rect = QRect(rect.left(), y, rect.width(), path_metrics.height())

        # The item view style ignores the disabled state for text we draw ourselves.
        group = (
            QPalette.ColorGroup.Normal
            if option.state & QStyle.StateFlag.State_Enabled
            else QPalette.ColorGroup.Disabled
        )
        painter.save()
        painter.setFont(self._name_font)
        painter.setPen(option.palette.color(group, QPalette.ColorRole.Text))
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignCenter, Path(path_str).stem)
        painter.setFont(option.font)
        painter.setPen(option.palette.color(group, QPalette.ColorRole.PlaceholderText))
        painter.drawText(path_rect, Qt.AlignmentFlag.AlignCenter, paths.contract_user_path(path_str))
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        path_str = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(path_str, str):
            return super().sizeHint(option, index)

        name_metrics = QFontMetrics(self._name_font)
        path_metrics = QFontMetrics(option.font)
        height = name_metrics.height() + path_metrics.height() + self._SPACING + 2 * self._MARGIN
        return QSize(0, height)
