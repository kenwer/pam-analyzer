"""Tree model adapter for AnalysisRunResult.

Rows:
- Optional project row (only for multi-campaign runs): exposes combined and
  summary CSVs in its 'Files' widget.
- One campaign row per CampaignRunResult.
- Optional week rows under each campaign row.

Column 0 holds the row label; column 1 is rendered via setIndexWidget in
BirdNetPanel so the model emits an empty string for it.
"""

from collections.abc import Iterator
from pathlib import Path

from PySide6.QtCore import QModelIndex
from PySide6.QtGui import QStandardItem, QStandardItemModel

from ...domain import AnalysisRunResult, CampaignRunResult, WeekRunResult

_ROLE_FOLDER = 0x100  # Qt.UserRole
_ROLE_FILES = 0x101


class BirdnetResultsModel(QStandardItemModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setHorizontalHeaderLabels(["Name", "Files"])

    def set_result(self, result: AnalysisRunResult) -> None:
        self.clear()
        self.setHorizontalHeaderLabels(["Name", "Files"])
        root = self.invisibleRootItem()

        if len(result.campaigns) > 1:
            project_item = QStandardItem("Project totals")
            project_files: list[Path] = [
                p
                for p in (
                    result.combined_csv,
                    result.per_campaign_aru_csv,
                    result.all_campaigns_csv,
                )
                if p is not None
            ]
            folder = result.campaigns[0].output_dir.parent if result.campaigns else Path(".")
            project_item.setData(folder, _ROLE_FOLDER)
            project_item.setData(project_files, _ROLE_FILES)
            root.appendRow([project_item, QStandardItem("")])
            self._append_campaigns(project_item, result.campaigns)
        else:
            self._append_campaigns(root, result.campaigns)

    def _append_campaigns(self, parent: QStandardItem, campaigns: tuple[CampaignRunResult, ...]) -> None:
        for c in campaigns:
            label = f"{c.campaign_name}  ({c.detection_count:,} detections)"
            item = QStandardItem(label)
            files: list[Path] = [c.detections_csv, c.per_aru_csv, c.all_arus_csv]
            if c.species_list_txt is not None:
                files.append(c.species_list_txt)
            item.setData(c.output_dir, _ROLE_FOLDER)
            item.setData(files, _ROLE_FILES)
            parent.appendRow([item, QStandardItem("")])
            for w in c.week_results:
                self._append_week(item, w)

    def _append_week(self, parent: QStandardItem, week: WeekRunResult) -> None:
        item = QStandardItem(f"Week {week.week:02d}")
        files: list[Path] = [
            week.detections_csv,
            week.per_aru_csv,
            week.all_arus_csv,
        ]
        if week.species_list_txt is not None:
            files.append(week.species_list_txt)
        item.setData(week.detections_csv.parent, _ROLE_FOLDER)
        item.setData(files, _ROLE_FILES)
        parent.appendRow([item, QStandardItem("")])

    def iter_file_rows(self) -> Iterator[tuple[QModelIndex, Path, list[Path]]]:
        """Yield (index_of_column_1, folder, files) for every populated row."""

        def walk(parent: QStandardItem) -> Iterator[QStandardItem]:
            for r in range(parent.rowCount()):
                child = parent.child(r, 0)
                if child is None:
                    continue
                yield child
                yield from walk(child)

        root = self.invisibleRootItem()
        for item in walk(root):
            folder = item.data(_ROLE_FOLDER)
            files = item.data(_ROLE_FILES) or []
            if folder is None:
                continue
            parent_item = item.parent()
            parent_index = parent_item.index() if parent_item is not None else QModelIndex()
            files_index = self.index(item.row(), 1, parent_index)
            yield files_index, folder, list(files)
