import asyncio
import shutil
import time
from pathlib import Path

from nicegui import run, ui

from pam_analyzer.core.campaign_settings import (
    CampaignSettings,
    discover_campaigns,
)
from pam_analyzer.core.sdcard_scanner import (
    CardResult,
    SDCardScanner,
    files_are_identical,
    resolve_dest_path,
)
from pam_analyzer.core.utils import (
    birdnet_week_from_wav,
    contract_user_path,
    eject_sd_card,
    format_eta,
    open_native_file_manager,
    scan_sdcard_files,
)
from pam_analyzer.panels.project import project_settings


class ImportAudioPanel:
    def __init__(self) -> None:
        self._scanner = SDCardScanner()
        self._watching = False
        self._poll_timer: ui.timer | None = None
        self._copying = False
        self._overwrite = False
        self._clear_after_copy = False

        # Campaign state
        self._campaign_dir: Path | None = None  # selected campaign
        self._campaign_paths: dict[str, Path] = {}

        # Current copy progress
        self._cur_done = 0
        self._cur_total = 0
        self._cur_start = 0.0

        # Session results
        self._results: list[CardResult] = []

        # UI refs (assigned in build)
        self._campaign_select: ui.select | None = None
        self._watch_btn: ui.button | None = None
        self._copying_section: ui.column | None = None
        self._cur_card_label: ui.label | None = None
        self._progress_bar: ui.linear_progress | None = None
        self._progress_label: ui.label | None = None
        self._eta_label: ui.label | None = None
        self._queue_row: ui.row | None = None
        self._queue_label: ui.label | None = None
        self._summary_label: ui.label | None = None
        self._log_column: ui.column | None = None
        self._campaign_info_label: ui.label | None = None
        self._watch_hint_label: ui.label | None = None

    def build(self) -> None:
        ui.label('Import Audio').classes('text-h5')

        # Campaign selector section
        ui.label('Campaign').classes('text-subtitle1 font-medium mt-2')
        with ui.row().classes('items-center gap-3'):
            self._campaign_select = ui.select(
                options=[],
                label='Select your Campaign',
                on_change=self._on_campaign_change,
            ).classes('w-64').props('outlined dense options-dense clearable')
            self._campaign_info_label = ui.label('').classes('text-caption text-grey self-center')

        self.refresh_campaigns()

        ui.separator().classes('my-3')

        # SD card watch controls section
        ui.label('SD Card Watching').classes('text-subtitle1 font-medium')
        self._watch_btn = ui.button('● Start watching for SD cards', on_click=self._toggle_watching)
        self._update_watch_btn_state()

        with ui.row().classes('items-center gap-4 mt-1'):
            ui.checkbox('Overwrite existing').bind_value(self, '_overwrite').props('dense').classes('text-caption')
            ui.checkbox('Clear card after copy').bind_value(self, '_clear_after_copy').props('dense').classes('text-caption')

        self._watch_hint_label = ui.label('Select a campaign, then start watching to automatically copy audio files when an SD card is inserted').classes('text-caption text-grey mt-1')

        # Copying section, visible only while a card is being copied
        with ui.column().classes('w-full gap-1 mt-4') as self._copying_section:
            ui.label('Copying').classes('text-caption text-grey')
            with ui.row().classes('w-full items-center gap-3'):
                self._cur_card_label = ui.label('').classes('font-mono font-bold')
                self._progress_bar = ui.linear_progress(value=0, show_value=False).props('rounded color=primary').classes('flex-1')
                self._progress_label = ui.label('').classes('text-caption w-32 text-right')
                self._eta_label = ui.label('').classes('text-caption text-grey w-16 text-right')
        self._copying_section.set_visibility(False)

        # Queue row, visible only when cards are waiting
        with ui.row().classes('items-center gap-2 mt-2') as self._queue_row:
            ui.label('Next:').classes('text-caption text-grey')
            self._queue_label = ui.label('').classes('text-caption')
        self._queue_row.set_visibility(False)

        ui.separator().classes('my-3')

        # Log section
        self._summary_label = ui.label('').classes('text-caption text-grey')
        self._summary_label.set_visibility(False)

        self._log_column = ui.column().classes('w-full gap-1 font-mono text-caption mt-2')
        self._log_column.set_visibility(False)

    # Watch toggle
    def _toggle_watching(self) -> None:
        if self._watching:
            self._stop_watching()
        else:
            self._start_watching()

    def _start_watching(self) -> None:
        self._watching = True
        self._scanner.reset()
        self._watch_btn.set_text('■ Stop watching for SD cards')
        self._watch_btn.props(remove='outline').props('color=negative')
        self._campaign_select.set_enabled(False)
        self._watch_hint_label.set_text('Once all imports are finished, stop watching to prevent unintended imports.')
        self._poll_timer = ui.timer(2.0, self._poll_sd_cards)

    def _stop_watching(self) -> None:
        self._watching = False
        self._watch_btn.set_text('● Start watching for SD cards')
        self._watch_btn.props(remove='color=negative').props('color=primary')
        self._campaign_select.set_enabled(True)
        self._watch_hint_label.set_text('Select a campaign, then start watching to automatically copy audio files when an SD card is inserted.')
        if self._poll_timer:
            self._poll_timer.cancel()
            self._poll_timer = None

    # Campaign management
    def _update_watch_btn_state(self) -> None:
        self._watch_btn.set_enabled(self._campaign_dir is not None)

    def refresh_campaigns(self) -> None:
        self._campaign_paths = discover_campaigns(Path(project_settings.audio_recordings_path))
        self._campaign_select.options = list(self._campaign_paths.keys())
        self._campaign_select.update()

    def _on_campaign_change(self, e) -> None:
        self._campaign_dir = self._campaign_paths.get(e.value) if e.value else None
        self._scanner.clear_seen()
        self._update_watch_btn_state()
        if self._campaign_dir is not None:
            settings = CampaignSettings.load(self._campaign_dir)
            info = f'Location: {settings.latitude}, {settings.longitude}' if settings.species_filter_mode == 'location' else 'Species list'
            self._campaign_info_label.set_text(info)
        else:
            self._campaign_info_label.set_text('')

    # Poll for SD cards
    async def _poll_sd_cards(self) -> None:
        self._scanner.poll(project_settings.sdcard_name_pattern)
        self._update_queue_ui()
        if not self._copying and self._scanner.has_pending:
            asyncio.create_task(self._process_next_sd_card())

    # Copy orchestration
    async def _process_next_sd_card(self) -> None:
        if self._copying or not self._scanner.has_pending:
            return
        self._copying = True
        entry = self._scanner.pop_next()
        if entry is None:
            self._copying = False
            return
        card_name, mountpoint, device = entry
        self._update_queue_ui()

        self._cur_done = 0
        self._cur_total = 0
        self._cur_start = time.monotonic()
        self._cur_card_label.set_text(card_name)
        self._progress_bar.set_value(0)
        self._progress_label.set_text('')
        self._eta_label.set_text('')
        self._copying_section.set_visibility(True)

        result = CardResult(card=card_name)
        try:
            result = await self._copy_card(card_name, mountpoint)
        except Exception as exc:
            result.error = str(exc)

        self._copying_section.set_visibility(False)
        self._results.append(result)
        self._update_summary_ui()

        if not result.error:
            try:
                await run.io_bound(eject_sd_card, mountpoint, device)
            except Exception:
                pass
            self._scanner.forget(card_name)

        self._copying = False
        if self._scanner.has_pending:
            asyncio.create_task(self._process_next_sd_card())

    async def _copy_card(self, card_name: str, mountpoint: str) -> CardResult:
        src_dir = Path(mountpoint)
        dest_dir = self._campaign_dir / card_name
        await run.io_bound(lambda: dest_dir.mkdir(parents=True, exist_ok=True))

        files = await run.io_bound(scan_sdcard_files, src_dir)
        self._cur_total = len(files)
        self._update_progress_ui()

        result = CardResult(card=card_name, dest_dir=dest_dir)
        conflict_policy: str | None = None  # 'skip' | 'replace' | None = ask

        for src in files:
            week = await run.io_bound(birdnet_week_from_wav, src) if src.name.upper() != 'CONFIG.TXT' else 0
            dest = await run.io_bound(resolve_dest_path, src, dest_dir, week)
            src_stat = await run.io_bound(src.stat)

            if dest.exists() and not self._overwrite:
                dest_stat = await run.io_bound(dest.stat)
                if files_are_identical(src_stat, dest_stat):
                    result.files_skipped += 1
                    self._cur_done += 1
                    self._update_progress_ui()
                    continue

                if conflict_policy is None:
                    decision = await self._ask_conflict(card_name, dest, src_stat, dest_stat)
                    # 'skip'/'replace' = once; 'skip_all'/'replace_all' = all remaining
                    if decision.endswith('_all'):
                        conflict_policy = decision[:-4]  # strip '_all'
                    effective = decision.replace('_all', '')
                else:
                    effective = conflict_policy

                if effective == 'skip':
                    result.files_skipped += 1
                    self._cur_done += 1
                    self._update_progress_ui()
                    continue

            await run.io_bound(shutil.copy2, src, dest)
            result.files_copied += 1
            result.bytes_copied += src_stat.st_size
            self._cur_done += 1
            self._update_progress_ui()

        result.elapsed = time.monotonic() - self._cur_start

        if self._clear_after_copy:
            for src in files:
                await run.io_bound(src.unlink, missing_ok=True)

        return result

    async def _ask_conflict(self, card_name: str, dest: Path, src_stat, dest_stat) -> str:
        """Show conflict dialog; returns 'skip', 'skip_all', 'replace', or 'replace_all'."""
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        def pick(value: str) -> None:
            dialog.close()
            future.set_result(value)

        dialog = ui.dialog().props('persistent')
        with dialog, ui.card().classes('gap-4 min-w-96'):
            ui.label('File already exists').classes('text-h6')
            ui.label(f'{card_name} → {dest.name}').classes('font-mono text-caption text-grey')

            with ui.grid(columns=3).classes('text-caption gap-x-6 gap-y-1'):
                ui.label('')
                ui.label('Size').classes('font-bold')
                ui.label('Modified').classes('font-bold')
                ui.label('Existing').classes('text-grey')
                ui.label(f'{dest_stat.st_size / 1e6:.1f} MB')
                ui.label(time.strftime('%d %b %Y  %H:%M', time.localtime(dest_stat.st_mtime)))
                ui.label('Incoming').classes('text-grey')
                ui.label(f'{src_stat.st_size / 1e6:.1f} MB')
                ui.label(time.strftime('%d %b %Y  %H:%M', time.localtime(src_stat.st_mtime)))

            with ui.row().classes('gap-2 justify-end w-full flex-wrap'):
                ui.button('Skip', on_click=lambda: pick('skip')).props('flat')
                ui.button('Skip all', on_click=lambda: pick('skip_all')).props('flat')
                ui.button('Replace', on_click=lambda: pick('replace')).props('outline')
                ui.button('Replace all', on_click=lambda: pick('replace_all')).props('color=primary')

        dialog.open()
        return await future

    # UI update helpers
    def _update_progress_ui(self) -> None:
        if self._cur_total == 0:
            return
        pct = self._cur_done / self._cur_total
        self._progress_bar.set_value(pct)
        self._progress_label.set_text(f'{self._cur_done} / {self._cur_total} files')

        elapsed = time.monotonic() - self._cur_start
        if elapsed > 1:
            self._eta_label.set_text(format_eta(self._cur_done, self._cur_total, elapsed))

    def _update_queue_ui(self) -> None:
        if self._scanner.queue:
            names = ' · '.join(name for name, *_ in self._scanner.queue[:6])
            if len(self._scanner.queue) > 6:
                names += f' (+{len(self._scanner.queue) - 6} more)'
            self._queue_label.set_text(names)
            self._queue_row.set_visibility(True)
        else:
            self._queue_row.set_visibility(False)

    def _update_summary_ui(self) -> None:
        error_count = sum(1 for r in self._results if r.error)
        done_count = len(self._results) - error_count

        parts = []
        if done_count:
            parts.append(f'✓ {done_count} done')
        if error_count:
            parts.append(f'! {error_count} error{"s" if error_count != 1 else ""}')
        self._summary_label.set_text('  '.join(parts))
        self._summary_label.set_visibility(bool(self._results))

        self._log_column.set_visibility(bool(self._results))
        self._log_column.clear()
        with self._log_column:
            for r in self._results:
                if r.error:
                    ui.label(f'! {r.card} · {r.error}').classes('text-negative')
                else:
                    def _num(val: object, width: int = 5) -> str:
                        return (
                            f'<span style="display:inline-block;min-width:{width}ch;'
                            f'font-variant-numeric:tabular-nums;text-align:right;'
                            f'padding-right:0.5ch;opacity:0.5">{val}</span>'
                        )
                    def _lbl(text: str) -> str:
                        return f'<span style="opacity:0.5"> {text} </span>'

                    mb = r.bytes_copied / 1e6
                    mbs = mb / r.elapsed if r.elapsed else 0
                    mins, secs = divmod(int(r.elapsed), 60)
                    with ui.row().classes('items-center gap-0'):
                        ui.html(
                            f'✓ {r.card}'
                            f'{_num(r.files_copied, width=6)}{_lbl("copied")}'
                            f'{_num(r.files_skipped, width=6)}{_lbl("skipped")}'
                            f'{_num(f"{mb:.1f}", width=7)}{_lbl("MB")}'
                            f'{_num(f"{mbs:.1f}", width=7)}{_lbl("MB/s")}'
                            f'{_num(f"{mins}m {secs:02d}s", width=8)}'
                            f'{_lbl("[ejected]")}'
                        )
                        if r.dest_dir:
                            dest = r.dest_dir
                            ui.label(contract_user_path(str(dest))) \
                                .classes('text-blue-500 cursor-pointer ml-2') \
                                .on('click', lambda d=dest: open_native_file_manager(str(d)))
