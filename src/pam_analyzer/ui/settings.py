"""User-level persistent settings (QSettings wrapper).

Tracks recent projects and window geometry across sessions. Project content
itself is stored in `.pamproj` TOML files. This module only holds UI/host state.
"""

from typing import TypeVar, cast

from PySide6.QtCore import QByteArray, QDir, QSettings

from .models.detections_table_model import DEFAULT_HIDDEN_COLUMNS

T = TypeVar("T")


class AppSettings:
    ORGANIZATION = "PAM Analyzer"
    APPLICATION = "PAM Analyzer"

    GROUP_UI = "ui"
    GROUP_RECENT = "recent"
    GROUP_EXAMINE = "examine"
    GROUP_CAMPAIGNS = "campaigns"

    KEY_WINDOW_GEOMETRY = "geometry"
    KEY_RECENT_PROJECTS = "projects"
    KEY_HIDDEN_COLUMNS = "hidden_columns"
    KEY_CAMPAIGN_SORT_ORDER = "sort_order"
    MAX_RECENT_PROJECTS = 8
    DEFAULT_CAMPAIGN_SORT_ORDER = "name_asc"

    def __init__(self) -> None:
        self._settings = QSettings(self.ORGANIZATION, self.APPLICATION)

    # recent projects

    @property
    def recent_projects(self) -> list[str]:
        self._settings.beginGroup(self.GROUP_RECENT)
        value = self._settings.value(self.KEY_RECENT_PROJECTS, [], type=list)
        self._settings.endGroup()
        return cast(list[str], value)

    def add_recent_project(self, path: str) -> None:
        recent = self.recent_projects
        if path in recent:
            recent.remove(path)
        recent.insert(0, path)
        recent = recent[: self.MAX_RECENT_PROJECTS]
        self._settings.beginGroup(self.GROUP_RECENT)
        self._settings.setValue(self.KEY_RECENT_PROJECTS, recent)
        self._settings.endGroup()

    def clear_recent_projects(self) -> None:
        self._settings.beginGroup(self.GROUP_RECENT)
        self._settings.remove(self.KEY_RECENT_PROJECTS)
        self._settings.endGroup()

    @property
    def last_directory(self) -> str:
        recent = self.recent_projects
        return str(recent[0]).rsplit("/", 1)[0] if recent else QDir.homePath()

    # window geometry

    @property
    def window_geometry(self) -> QByteArray | None:
        self._settings.beginGroup(self.GROUP_UI)
        value = (
            cast(QByteArray, self._settings.value(self.KEY_WINDOW_GEOMETRY, type=QByteArray))
            if self._settings.contains(self.KEY_WINDOW_GEOMETRY)
            else None
        )
        self._settings.endGroup()
        return value

    @window_geometry.setter
    def window_geometry(self, value: QByteArray) -> None:
        self._settings.beginGroup(self.GROUP_UI)
        self._settings.setValue(self.KEY_WINDOW_GEOMETRY, value)
        self._settings.endGroup()

    # examine panel state

    @property
    def examine_hidden_columns(self) -> list[str]:
        """Names of columns the user has hidden in the Examine panel."""
        self._settings.beginGroup(self.GROUP_EXAMINE)
        value = self._settings.value(self.KEY_HIDDEN_COLUMNS, sorted(DEFAULT_HIDDEN_COLUMNS), type=list)
        self._settings.endGroup()
        return cast(list[str], value)

    @examine_hidden_columns.setter
    def examine_hidden_columns(self, value: list[str]) -> None:
        self._settings.beginGroup(self.GROUP_EXAMINE)
        self._settings.setValue(self.KEY_HIDDEN_COLUMNS, list(value))
        self._settings.endGroup()

    # campaigns panel state

    @property
    def campaign_sort_order(self) -> str:
        """Value of the CampaignSortOrder the user last picked from the list's context menu."""
        self._settings.beginGroup(self.GROUP_CAMPAIGNS)
        value = self._settings.value(
            self.KEY_CAMPAIGN_SORT_ORDER, self.DEFAULT_CAMPAIGN_SORT_ORDER, type=str
        )
        self._settings.endGroup()
        return cast(str, value)

    @campaign_sort_order.setter
    def campaign_sort_order(self, value: str) -> None:
        self._settings.beginGroup(self.GROUP_CAMPAIGNS)
        self._settings.setValue(self.KEY_CAMPAIGN_SORT_ORDER, value)
        self._settings.endGroup()
