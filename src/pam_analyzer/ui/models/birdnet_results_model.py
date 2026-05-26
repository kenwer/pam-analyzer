"""Tree model adapter for AnalysisRunResult.

One row per CampaignRunResult, exposing that campaign's detections CSV (and,
in location mode, its geographic species-list .txt) in the 'Files' widget.

Column 0 holds the row label; column 1 is rendered via setIndexWidget in
BirdNetPanel so the model emits an empty string for it.
"""

from collections.abc import Iterator
from pathlib import Path

from PySide6.QtCore import QModelIndex
from PySide6.QtGui import QStandardItem, QStandardItemModel

from ...domain import AnalysisRunResult, CampaignRunResult

_ROLE_FOLDER = 0x100  # Qt.UserRole
_ROLE_FILES = 0x101


class BirdnetResultsModel(QStandardItemModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setHorizontalHeaderLabels(["Name", "Files"])

    def set_result(self, result: AnalysisRunResult) -> None:
        self.clear()
        self.setHorizontalHeaderLabels(["Name", "Files"])
        self._append_campaigns(self.invisibleRootItem(), result.campaigns)

    def _append_campaigns(self, parent: QStandardItem, campaigns: tuple[CampaignRunResult, ...]) -> None:
        for c in campaigns:
            model_name = f"{c.model_key}" if c.model_key else ""
            label = f"{c.campaign_name} · {c.detection_count:,} detections via {model_name}"
            item = QStandardItem(label)
            files: list[Path] = [c.detections_csv]
            if c.species_list_txt is not None:
                files.append(c.species_list_txt)
            item.setData(c.output_dir, _ROLE_FOLDER)
            item.setData(files, _ROLE_FILES)
            parent.appendRow([item, QStandardItem("")])

    def iter_file_rows(self) -> Iterator[tuple[QModelIndex, Path, list[Path]]]:
        """Yield (index_of_column_1, folder, files) for every populated row."""
        root = self.invisibleRootItem()
        for row in range(root.rowCount()):
            item = root.child(row, 0)
            if item is None:
                continue
            folder = item.data(_ROLE_FOLDER)
            if folder is None:
                continue
            files = item.data(_ROLE_FILES) or []
            files_index = self.index(item.row(), 1, QModelIndex())
            yield files_index, folder, list(files)
