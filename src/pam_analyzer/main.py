import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path

# In PyInstaller frozen builds every process (including BirdNET multiprocessing
# workers) re-executes __main__. Calling freeze_support() here (before any
# heavy imports) lets worker processes exit immediately without loading the
# full GUI stack. See https://github.com/zauberzeug/nicegui/issues/5684
if getattr(sys, 'frozen', False):
    import multiprocessing

    multiprocessing.freeze_support()

from platformdirs import user_data_dir

# Must be set before importing nicegui so storage initializes to the correct path.
_storage_path = Path(user_data_dir('pam-analyzer'))
_storage_path.mkdir(parents=True, exist_ok=True)
os.environ['NICEGUI_STORAGE_PATH'] = str(_storage_path)
os.environ['NICEGUI_INCLUDE_MATPLOTLIB'] = 'false'  # not used in this app; avoids loading matplotlib on startup (see https://github.com/zauberzeug/nicegui/issues/5684)

# We set an env var before importing, this is intentional and required
from nicegui import app, ui  # noqa: E402
from webview import FileDialog  # noqa: E402

from pam_analyzer.app_settings import add_to_recent, get_recent_projects  # noqa: E402
from pam_analyzer.panels.about_dialog import show_about_dialog  # noqa: E402
from pam_analyzer.panels.birdnet import BirdNETPanel  # noqa: E402
from pam_analyzer.panels.campaigns import CampaignsPanel  # noqa: E402
from pam_analyzer.panels.examine import ExaminePanel  # noqa: E402
from pam_analyzer.panels.import_audio import ImportAudioPanel  # noqa: E402
from pam_analyzer.panels.project import (  # noqa: E402
    ProjectPanel,
    get_project_path,
    is_project_loaded,
    load_project,
    new_project,
    project_status,
    reset_project,
    save_project_as,
)


class KeyboardShortcuts:
    """Registers keyboard shortcuts and generates platform-aware display labels.

    Primary modifier is Cmd (⌘) on macOS and Ctrl on Windows/Linux.
    Handlers may be sync or async; async handlers are awaited automatically.
    """

    _IS_MAC: bool = sys.platform == 'darwin'

    def __init__(self) -> None:
        self._handlers: dict[tuple[str, bool], Callable] = {}
        # ignore=[] so shortcuts fire even when an input field is focused (e.g. Cmd+S to save)
        ui.keyboard(on_key=self._on_key, ignore=[])

    def register(self, key: str, handler: Callable, *, shift: bool = False) -> None:
        self._handlers[(key.lower(), shift)] = handler

    @classmethod
    def label(cls, key: str, *, shift: bool = False) -> str:
        """Return a display string for use in menus, e.g. '⌘S' or 'Ctrl+S'."""
        if cls._IS_MAC:
            return ('⇧' if shift else '') + '⌘' + key.upper()
        return ('Ctrl+Shift+' if shift else 'Ctrl+') + key.upper()

    async def _on_key(self, e) -> None:
        if not e.action.keydown:
            return
        primary = e.modifiers.meta if self._IS_MAC else e.modifiers.ctrl
        if not primary:
            return
        handler = self._handlers.get((e.key.name.lower(), e.modifiers.shift))
        if handler:
            result = handler()
            if asyncio.iscoroutine(result):
                await result


def _menu_item(title: str, handler: Callable, key: str, *, shift: bool = False) -> None:
    """Render a menu item with a platform-aware keyboard shortcut hint on the right."""
    with ui.menu_item(title, on_click=handler):
        with ui.item_section().props('side'):
            ui.label(KeyboardShortcuts.label(key, shift=shift)).classes('text-caption text-grey-5')


async def run_open_dialog() -> bool:
    """Show the native open-file dialog, load the selected project, and return ``True`` on success.

    Returns ``False`` if the user cancels without selecting a file.
    Must be awaited from an async NiceGUI event handler.
    """
    result = await app.native.main_window.create_file_dialog(
        FileDialog.OPEN,
        file_types=(
            'PAM Project (*.pamproj)',
            'All files (*.*)',
        ),
    )
    if result:
        path = Path(result[0])
        load_project(path)
        add_to_recent(path)
        return True
    return False


def on_new_project() -> None:
    """Handle the *New Project* action: create an empty in-memory project and enter the main UI."""
    new_project()
    ui.navigate.reload()


async def on_open() -> None:
    """Handle the *Open* action: show the file dialog and reload the UI if a project was selected."""
    if await run_open_dialog():
        ui.navigate.reload()


def on_close_project() -> None:
    """Handle the *Close Project* action: reset state and return to the welcome screen."""
    reset_project()
    ui.navigate.reload()


def open_recent_project(path: Path) -> None:
    """Load a recently opened project and reload the UI."""
    load_project(path)
    add_to_recent(path)
    ui.navigate.reload()


async def on_save() -> None:
    """Handle the *Save Project* action: save to the current path, or prompt if unsaved."""
    path = get_project_path()
    if path:
        save_project_as(path)
        ui.notify(f'Saved: {path.name}', type='positive')
    else:
        await on_save_as()


async def on_save_as() -> None:
    """Handle the *Save As* action: prompt for a new file location and save the project there."""
    result = await app.native.main_window.create_file_dialog(
        FileDialog.SAVE,
        save_filename=get_project_path().name if get_project_path() else 'project.pamproj',
        file_types=(
            'PAM Project (*.pamproj)',
            'All files (*.*)',
        ),
    )
    if result:
        path = Path(result[0] if isinstance(result, (list, tuple)) else result)
        save_project_as(path)
        add_to_recent(path)
        ui.notify(f'Saved: {path.name}', type='positive')


def build_welcome() -> None:
    """Render the welcome screen shown before any project is loaded.

    Provides buttons to create a new project or open an existing one, plus a
    list of recently opened projects for quick access.
    """

    async def on_open_project() -> None:
        if await run_open_dialog():
            ui.navigate.reload()

    recents = [p for p in get_recent_projects() if Path(p).exists()]

    with ui.column().classes('absolute-center items-center gap-6 text-center'):
        ui.icon('audio_file', size='4rem').classes('text-primary')
        ui.label('PAM Analyzer').classes('text-h4 font-bold')
        ui.label('Open an existing project or create a new one to get started.').classes('text-gray-500')
        with ui.row().classes('gap-4 mt-2'):
            ui.button('New Project', icon='add', on_click=on_new_project).props('size=lg color=primary')
            ui.button('Open Project', icon='folder_open', on_click=on_open_project).props('size=lg outline')

        if recents:
            ui.separator().classes('w-80')
            ui.label('Recent Projects').classes('text-sm font-semibold text-gray-500')
            with ui.column().classes('gap-1 w-80'):
                for p in recents:
                    path = Path(p)

                    with ui.item(on_click=lambda p=path: open_recent_project(p)).classes('w-full rounded cursor-pointer'):
                        with ui.item_section():
                            ui.item_label(path.stem).classes('font-medium')
                            ui.item_label(str(path)).props('caption').classes('text-xs text-gray-400')


def build_main_content() -> None:
    """Render the main application UI: project header bar and vertical tab navigation."""
    recents = [Path(p) for p in get_recent_projects() if Path(p).exists()]

    with ui.column().classes('w-full h-full gap-0'):
        with ui.row().classes('w-full items-center gap-0 px-1 py-0 flex-none border-b'):
            with ui.button('File').props('flat no-caps'):
                with ui.menu():
                    _menu_item('New Project', on_new_project, 'n')
                    _menu_item('Open Project...', on_open, 'o')
                    with ui.menu_item('Open Recent Projects', auto_close=False).props('' if recents else 'disable'):
                        with ui.item_section().props('side'):
                            ui.icon('keyboard_arrow_right')
                        if recents:
                            with ui.menu().props('anchor="top end" self="top start" auto-close'):
                                for path in recents:
                                    ui.menu_item(
                                        path.stem,
                                        on_click=lambda p=path: open_recent_project(p),
                                    ).tooltip(str(path))
                    _menu_item('Save Project', on_save, 's')
                    _menu_item('Save Project As...', on_save_as, 's', shift=True)
                    _menu_item('Close Project', on_close_project, 'w')
                    ui.separator()
                    ui.menu_item('About', on_click=show_about_dialog)
                    ui.separator()
                    _menu_item('Quit', app.shutdown, 'q')
        with ui.row().classes('w-full gap-0 flex-nowrap flex-1'):
            with ui.tabs().props('vertical').classes('flex-none') as tabs:
                project_tab = ui.tab('Project', icon='settings')
                campaigns_tab = ui.tab('Campaigns', icon='folder_special')
                import_audio = ui.tab('Import', icon='sd_card')
                run_birdnet = ui.tab('BirdNET', icon='flutter_dash')
                examine_data = ui.tab('Examine', icon='data_thresholding')
            for tab in (campaigns_tab, import_audio, run_birdnet, examine_data):
                tab.bind_enabled(project_status, 'ready')
            campaigns_panel = CampaignsPanel()
            birdnet_panel = BirdNETPanel()
            import_panel = ImportAudioPanel()
            examine_panel = ExaminePanel()

            async def _on_tab_change(e) -> None:
                # Re-discover campaigns when navigating to a panel that depends on them
                if e.value == 'Campaigns':
                    campaigns_panel.refresh_campaigns()
                elif e.value == 'Import':
                    import_panel.refresh_campaigns()
                elif e.value == 'BirdNET':
                    birdnet_panel.refresh_campaigns()
                elif e.value == 'Examine':
                    await examine_panel.refresh()

            with ui.tab_panels(tabs, value=project_tab, on_change=_on_tab_change).props('vertical').classes('flex-1 h-full'):
                with ui.tab_panel(project_tab):
                    ProjectPanel().build()
                with ui.tab_panel(campaigns_tab):
                    campaigns_panel.build()
                with ui.tab_panel(import_audio):
                    import_panel.build()
                with ui.tab_panel(run_birdnet):
                    birdnet_panel.build()
                with ui.tab_panel(examine_data):
                    examine_panel.build()


def build_ui() -> None:
    """Root UI builder registered with NiceGUI: routes to the welcome screen or main content."""
    # Give q-page a definite height so h-full/flex-1 chains propagate correctly.
    # Quasar only sets min-height on q-page (via JS), which does not count as a
    # definite height for CSS percentage resolution in children.
    ui.add_css("""
        .q-page { height: 100vh; overflow: hidden; }
        .nicegui-content { height: 100%; }
        .nicegui-splitter .q-splitter__panel { height: 100%; }
    """)

    shortcuts = KeyboardShortcuts()
    shortcuts.register('n', on_new_project)
    shortcuts.register('o', on_open)
    shortcuts.register('s', on_save)
    shortcuts.register('s', on_save_as, shift=True)
    shortcuts.register('w', on_close_project)
    shortcuts.register('q', app.shutdown)
    if is_project_loaded():
        build_main_content()
    else:
        build_welcome()


@app.on_connect
async def _close_splash() -> None:
    try:
        import pyi_splash  # type: ignore[import]  # only exists in PyInstaller builds

        pyi_splash.close()
    except ImportError:
        pass


@app.on_startup
async def _suppress_win_connection_reset() -> None:
    """Suppress spurious WinError 10054 on shutdown.

    On Windows, pywebview abruptly drops WebSocket connections when its window
    closes, causing asyncio's ProactorEventLoop to emit ConnectionResetError
    noise in the terminal.  This exception handler silences those and forwards
    everything else to the default handler.
    """
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda lp, ctx: None if isinstance(ctx.get('exception'), ConnectionResetError) else lp.default_exception_handler(ctx))


def main() -> None:
    """Application entry point: start the NiceGUI native window."""
    ui.run(
        root=build_ui,
        native=True,
        reload=False,
        title='PAM Analyzer',
        window_size=(1400, 900),
    )


if __name__ == '__main__':
    # Required for PyInstaller: without this guard, the bundled binary imports
    # everything and exits without ever calling main(). freeze_support() prevents
    # multiprocessing worker processes (used by BirdNET) from re-launching the GUI
    # on macOS and Windows, where the 'spawn' start method re-executes this script.
    import multiprocessing

    multiprocessing.freeze_support()
    main()
