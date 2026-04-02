from pathlib import Path

from nicegui import app

_RECENT_PROJECTS_KEY = 'recent_projects'
_RECENT_MAX = 8


def add_to_recent(path: Path) -> None:
    """Prepend *path* to the recent-projects list in NiceGUI general storage."""
    recents: list[str] = app.storage.general.get(_RECENT_PROJECTS_KEY, [])
    key = str(path)
    recents = [p for p in recents if p != key]
    recents.insert(0, key)
    app.storage.general[_RECENT_PROJECTS_KEY] = recents[:_RECENT_MAX]


def get_recent_projects() -> list[str]:
    """Return the list of recently opened project paths from NiceGUI general storage."""
    return app.storage.general.get(_RECENT_PROJECTS_KEY, [])
