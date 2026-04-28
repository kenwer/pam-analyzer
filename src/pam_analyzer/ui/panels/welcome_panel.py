"""Welcome screen shown when no project is loaded.

Offers buttons to create / open a project plus a list of recent projects.
The panel itself is stateless about persistence, it emits signal. The
main window owns AppSettings and the open/create handlers.
"""

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QLabel,
    QListWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import resources_rc  # noqa: F401  registers :/icons/* resources
from .ui_welcome_panel import Ui_WelcomePanel


class WelcomePanel(QWidget):
    newRequested = Signal()
    openRequested = Signal()
    recentRequested = Signal(str)  # path str

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ui = Ui_WelcomePanel()
        self.ui.setupUi(self)

        self.ui.icon_label.setPixmap(QIcon(":/icons/icon.svg").pixmap(QSize(112, 112)))

        self.ui.new_button.clicked.connect(self.newRequested.emit)
        self.ui.open_button.clicked.connect(self.openRequested.emit)
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
            row = _build_recent_row(path_str)
            item.setSizeHint(row.sizeHint())
            self.ui.recent_list.addItem(item)
            self.ui.recent_list.setItemWidget(item, row)

    def _on_recent_activated(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(path, str):
            self.recentRequested.emit(path)


def _build_recent_row(path_str: str) -> QWidget:
    row = QWidget()
    # Let clicks pass through to the underlying QListWidgetItem.
    row.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    layout = QVBoxLayout(row)
    layout.setContentsMargins(8, 6, 8, 6)
    layout.setSpacing(2)

    name = QLabel(Path(path_str).stem)
    name_font = QFont()
    name_font.setBold(True)
    name.setFont(name_font)
    name.setAlignment(Qt.AlignmentFlag.AlignCenter)

    path = QLabel(path_str)
    path.setStyleSheet("color: #9ca3af;")
    path.setAlignment(Qt.AlignmentFlag.AlignCenter)

    layout.addWidget(name)
    layout.addWidget(path)
    row.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    return row
