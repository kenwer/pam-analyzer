"""Dialog for confirming/renaming cards detected under a manually picked folder."""

import dataclasses

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTableWidgetItem, QWidget

from ...domain.audio_import import DetectedCard
from .ui_folder_import_dialog import Ui_FolderImportDialog


class FolderImportDialog(QDialog):
    def __init__(
        self, cards: list[DetectedCard], file_counts: list[int], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.ui = Ui_FolderImportDialog()
        self.ui.setupUi(self)

        self._cards = cards
        self._file_counts = file_counts
        self._populate_table()
        self.ui.card_table.itemChanged.connect(self._on_item_changed)
        self._validate()

    def _populate_table(self) -> None:
        table = self.ui.card_table
        table.setRowCount(len(self._cards))
        table.horizontalHeader().setStretchLastSection(False)

        for row, card in enumerate(self._cards):
            name_item = QTableWidgetItem(card.name)
            table.setItem(row, 0, name_item)

            files_item = QTableWidgetItem(str(self._file_counts[row]))
            files_item.setFlags(files_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 1, files_item)

        table.resizeColumnsToContents()

    def _on_item_changed(self, _item: QTableWidgetItem) -> None:
        self._validate()

    def _validate(self) -> None:
        names = [self.ui.card_table.item(row, 0).text().strip() for row in range(self.ui.card_table.rowCount())]

        error = ""
        for name in names:
            if not name or "/" in name or "\\" in name:
                error = f"Invalid card name: {name!r}"
                break
        else:
            seen: set[str] = set()
            for name in names:
                if name in seen:
                    error = f"Duplicate card name: {name!r}"
                    break
                seen.add(name)

        self.ui.validation_label.setText(error)
        self.ui.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not error)

    def result_cards(self) -> list[DetectedCard]:
        renamed = []
        for row, card in enumerate(self._cards):
            new_name = self.ui.card_table.item(row, 0).text().strip()
            renamed.append(dataclasses.replace(card, name=new_name))
        return renamed

    def clear_after(self) -> bool:
        return self.ui.clear_after_check.isChecked()
