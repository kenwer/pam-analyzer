"""Dialog for resolving file conflicts before an SD card import."""

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QDialog, QTableWidgetItem, QWidget

from ...domain.audio_import import ConflictChoice, FileConflict
from .ui_import_conflict_dialog import Ui_ImportConflictDialog

_SKIP_INDEX = 0
_REPLACE_INDEX = 1


class ImportConflictDialog(QDialog):
    def __init__(self, conflicts: list[FileConflict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ui = Ui_ImportConflictDialog()
        self.ui.setupUi(self)

        self._conflicts = conflicts
        self._combos: list[QComboBox] = []

        self._populate_table()
        self.ui.skip_all_button.clicked.connect(self._skip_all)
        self.ui.replace_all_button.clicked.connect(self._replace_all)

    def _populate_table(self) -> None:
        table = self.ui.conflict_table
        table.setRowCount(len(self._conflicts))
        table.horizontalHeader().setStretchLastSection(False)

        for row, conflict in enumerate(self._conflicts):
            existing_date = time.strftime("%d %b %Y %H:%M", time.localtime(conflict.dst_mtime))
            incoming_date = time.strftime("%d %b %Y %H:%M", time.localtime(conflict.src_mtime))

            for col, text in enumerate(
                [
                    conflict.filename,
                    f"{conflict.dst_size / 1e6:.1f} MB",
                    existing_date,
                    f"{conflict.src_size / 1e6:.1f} MB",
                    incoming_date,
                ]
            ):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(row, col, item)

            combo = QComboBox()
            combo.addItem("Skip", ConflictChoice.SKIP)
            combo.addItem("Replace", ConflictChoice.REPLACE)
            table.setCellWidget(row, 5, combo)
            self._combos.append(combo)

        table.resizeColumnsToContents()

    def _skip_all(self) -> None:
        for combo in self._combos:
            combo.setCurrentIndex(_SKIP_INDEX)

    def _replace_all(self) -> None:
        for combo in self._combos:
            combo.setCurrentIndex(_REPLACE_INDEX)

    def result_resolutions(self) -> dict[str, ConflictChoice]:
        return {
            conflict.filename: self._combos[i].currentData()
            for i, conflict in enumerate(self._conflicts)
        }
