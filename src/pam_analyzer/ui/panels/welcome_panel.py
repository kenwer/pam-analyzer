"""Welcome screen shown when no project is loaded.

Offers buttons to create / open a project plus a list of recent projects.
The panel itself is stateless about persistence, it emits signal. The
main window owns AppSettings and the open/create handlers.
"""

from pathlib import Path

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPalette
from PySide6.QtWidgets import (
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from ...infrastructure import paths
from .. import resources_rc  # noqa: F401  registers :/icons/* resources
from .ui_welcome_panel import Ui_WelcomePanel


class WelcomePanel(QWidget):
    newRequested = Signal()
    openProjectFolderRequested = Signal()
    recentRequested = Signal(str)  # path str

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ui = Ui_WelcomePanel()
        self.ui.setupUi(self)

        self.ui.icon_label.setPixmap(QIcon(":/icons/icon.svg").pixmap(QSize(112, 112)))
        self.ui.recent_list.setItemDelegate(_RecentProjectDelegate(self.ui.recent_list))

        self.ui.new_button.clicked.connect(self.newRequested.emit)
        self.ui.open_project_folder_button.clicked.connect(self.openProjectFolderRequested.emit)
        self.ui.recent_list.itemActivated.connect(self._on_recent_activated)
        self.ui.recent_list.itemClicked.connect(self._on_recent_activated)

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

    def _on_recent_activated(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(path, str):
            self.recentRequested.emit(path)


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

        painter.save()
        painter.setFont(self._name_font)
        painter.setPen(option.palette.color(QPalette.ColorRole.Text))
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignCenter, Path(path_str).stem)
        painter.setFont(option.font)
        painter.setPen(QColor("#9ca3af"))
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
