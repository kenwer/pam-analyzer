"""Campaign management panel.

Master-detail layout: scrollable campaign card list on the left,
inline create/edit form on the right.
"""

from pathlib import Path

from nicegui import ui

from pam_analyzer.core.campaign_settings import CampaignSettings, discover_campaigns
from pam_analyzer.panels.campaign_detail import CampaignDetail
from pam_analyzer.panels.project import project_settings


class CampaignsPanel:
    def __init__(self) -> None:
        self._campaign_paths: dict[str, Path] = {}
        self._selected_name: str | None = None
        self._campaign_list: ui.list | None = None
        self._details: CampaignDetail | None = None

    def build(self) -> None:
        ui.label('Manage Campaigns').classes('text-h5')

        self._details = CampaignDetail(
            on_saved=self._on_saved,
            on_deleted=self._on_deleted,
            on_cancelled=self._on_cancelled,
        )

        with ui.splitter(value=20).classes('w-full flex-1') as splitter:
            with splitter.before:
                with ui.row().classes('w-full items-center px-3 py-2 border-b gap-2'):
                    ui.label('Campaigns').classes('text-subtitle1 font-medium flex-1')
                    ui.button(icon='add', on_click=self._on_new_campaign).props('flat round dense').tooltip('New campaign')
                self._campaign_list = ui.list().props('separator').classes('w-full')
            with splitter.after:
                with ui.column().classes('p-4 gap-3 w-full h-full overflow-y-auto'):
                    self._details.build()

        self.refresh_campaigns()

    def refresh_campaigns(self) -> None:
        self._campaign_paths = discover_campaigns(Path(project_settings.audio_recordings_path))
        if self._selected_name and self._selected_name not in self._campaign_paths:
            self._selected_name = None
            if self._details:
                self._details.show_empty()
        self._rebuild_list()

    def _rebuild_list(self) -> None:
        if self._campaign_list is None:
            return
        self._campaign_list.clear()
        with self._campaign_list:
            if not self._campaign_paths:
                with ui.item():
                    with ui.item_section():
                        ui.item_label('No campaigns yet').classes('text-grey')
                        ui.item_label('Click + to create one').props('caption')
            else:
                for name, path in self._campaign_paths.items():
                    self._build_campaign_card(name, CampaignSettings.load(path))

    def _build_campaign_card(self, name: str, settings: CampaignSettings) -> None:
        is_selected = name == self._selected_name
        info = (
            f'\U0001f4cd {settings.latitude}, {settings.longitude}'
            if settings.species_filter_mode == 'location'
            else '\U0001f4cb Species list'
        )
        with ui.item(on_click=lambda n=name: self._select_campaign(n)).classes('cursor-pointer rounded' + (' bg-blue-50' if is_selected else '')):
            with ui.item_section():
                ui.item_label(name).classes('font-medium' + (' text-primary' if is_selected else ''))
                ui.item_label(info).props('caption')
            with ui.item_section().props('side'):
                (ui.button(icon='delete', on_click=lambda n=name: self._on_delete_campaign(n))
                 .props('flat round dense color=negative')
                 .tooltip('Delete campaign'))

    def _select_campaign(self, name: str) -> None:
        self._selected_name = name
        self._rebuild_list()
        self._details.open_edit(name, self._campaign_paths[name], self._campaign_paths)

    def _on_new_campaign(self) -> None:
        self._selected_name = None
        self._rebuild_list()
        self._details.open_new(self._campaign_paths)

    def _on_delete_campaign(self, name: str) -> None:
        self._selected_name = name
        self._rebuild_list()
        self._details.show_delete_confirm(name, self._campaign_paths)

    def _on_saved(self, new_name: str) -> None:
        self._selected_name = new_name
        self.refresh_campaigns()
        self._details.open_edit(new_name, self._campaign_paths[new_name], self._campaign_paths)

    def _on_deleted(self) -> None:
        self._selected_name = None
        self.refresh_campaigns()

    def _on_cancelled(self) -> None:
        self._selected_name = None
        self._rebuild_list()
