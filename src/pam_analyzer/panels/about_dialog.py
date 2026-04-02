"""About dialog for PAM Analyzer."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from nicegui import ui

_APP_NAME = 'PAM Analyzer'
_AUTHOR = 'Ken Werner'
_URL = 'https://github.com/kenwer/pam-analyzer'

try:
    _VERSION = version('pam-audio-extractor2')
except PackageNotFoundError:
    _VERSION = 'dev'


def _load_changelog() -> str:
    import sys

    # PyInstaller frozen build: CHANGELOG.md is extracted to _MEIPASS
    if getattr(sys, 'frozen', False):
        changelog = Path(sys._MEIPASS) / 'CHANGELOG.md'  # type: ignore[attr-defined]
        if changelog.exists():
            return changelog.read_text(encoding='utf-8')
        return '*No changelog available.*'
    # Dev/editable install: walk up from this file to find CHANGELOG.md
    candidate = Path(__file__).parent
    for _ in range(5):
        changelog = candidate / 'CHANGELOG.md'
        if changelog.exists():
            return changelog.read_text(encoding='utf-8')
        candidate = candidate.parent
    return '*No changelog available.*'


def show_about_dialog() -> None:
    with (
        ui.dialog() as dialog,
        ui.card().classes('p-6 gap-4').style('min-width: 800px; max-width: 1200px'),
    ):
        with ui.row().classes('items-center gap-8'):
            ui.image(Path(__file__).parent.parent / 'static' / 'app_icon.svg').style('width: 96px; height: 96px; flex-shrink: 0')

            with ui.column().classes('gap-2'):
                ui.label(_APP_NAME).classes('text-3xl font-light')
                ui.label(f'Version: {_VERSION}').classes('text-subtitle1')
                ui.label(f'Author: {_AUTHOR}').classes('text-subtitle1')
                ui.link(_URL, _URL).classes('text-primary')

        with ui.scroll_area().classes('w-full border rounded').style('height: 300px'):
            ui.markdown(_load_changelog())  # .classes("p-3")

        with ui.row().classes('justify-end w-full'):
            ui.button('Close', on_click=dialog.close).props('flat')

    dialog.open()
