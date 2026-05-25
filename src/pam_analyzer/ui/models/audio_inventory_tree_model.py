"""Tree model that renders one campaign's audio inventory as Card / Week / File rows.

Columns:
  0  Name        (Card -> Week NN -> filename)
  1  Files       (Card / Week: file count; File: empty)
  2  Size        (Card / Week / File: human-readable bytes)

The model is fed a CampaignInventory; pass None to clear. The folder/file
Path for each row is stashed via _ROLE_PATH so the panel can wire double-click
or context-menu actions later without recomputing.
"""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel

from ...domain import CampaignInventory, CardInventory, WeekInventory

_ROLE_PATH = int(Qt.ItemDataRole.UserRole) + 1

_UNSORTED_LABEL = "Unsorted"


class AudioInventoryTreeModel(QStandardItemModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setHorizontalHeaderLabels(["Name", "Files", "Size"])

    def set_campaign(self, campaign: CampaignInventory | None) -> None:
        self.clear()
        self.setHorizontalHeaderLabels(["Name", "Files", "Size"])
        if campaign is None:
            return
        root = self.invisibleRootItem()
        for card in campaign.cards:
            row = self._card_row(card)
            root.appendRow(row)
            card_parent = row[0]
            for week in card.weeks:
                card_parent.appendRow(self._week_row(week))

    def _card_row(self, card: CardInventory) -> list[QStandardItem]:
        name = QStandardItem(card.name)
        name.setData(card.folder, _ROLE_PATH)
        files = QStandardItem(str(card.file_count))
        size = QStandardItem(format_bytes(card.total_bytes))
        for item in (name, files, size):
            item.setEditable(False)
        return [name, files, size]

    def _week_row(self, week: WeekInventory) -> list[QStandardItem]:
        label = _UNSORTED_LABEL if week.week < 0 else f"Week {week.week:02d}"
        name = QStandardItem(label)
        # Week itself has no folder of its own here (files live under
        # card/week_NN/), but children get their per-file paths.
        files = QStandardItem(str(len(week.files)))
        size = QStandardItem(format_bytes(week.total_bytes))
        for item in (name, files, size):
            item.setEditable(False)
        for path, sz in zip(week.files, week.file_sizes, strict=True):
            name.appendRow(self._file_row(path, sz))
        return [name, files, size]

    def _file_row(self, path: Path, size: int) -> list[QStandardItem]:
        name = QStandardItem(path.name)
        name.setData(path, _ROLE_PATH)
        empty = QStandardItem("")
        size_item = QStandardItem(format_bytes(size))
        for item in (name, empty, size_item):
            item.setEditable(False)
        return [name, empty, size_item]


def format_bytes(n: int) -> str:
    """Render bytes as KB / MB / GB. Decimal thousands, since users think in MB."""
    if n < 1000:
        return f"{n} B"
    if n < 1_000_000:
        return f"{n / 1_000:.1f} KB"
    if n < 1_000_000_000:
        return f"{n / 1_000_000:.1f} MB"
    return f"{n / 1_000_000_000:.2f} GB"
