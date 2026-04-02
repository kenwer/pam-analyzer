import time
from pathlib import Path

from nicegui import run, ui

from pam_analyzer.core.birdnet_runner import (
    AnalysisSettings,
    count_wav_files,
    get_available_locales,
)
from pam_analyzer.core.campaign_settings import CampaignSettings, discover_campaigns
from pam_analyzer.core.utils import (
    contract_user_path,
    format_eta,
    get_project_name,
    open_file,
    open_native_file_manager,
)
from pam_analyzer.panels.project import (
    get_project_output_base,
    get_project_path,
    project_settings,
)

_ALL_CAMPAIGNS = 'all'  # sentinel value used as the campaign select key for "run all campaigns"


def _get_available_locales() -> list[str]:
    try:
        return get_available_locales()
    except Exception:
        return []


class BirdNETPanel:
    def __init__(self) -> None:
        self._campaign_dir: Path | None = None
        self._campaign_settings: CampaignSettings | None = None
        self._campaign_paths: dict[str, Path] = {}
        self._running = False
        self._total_wavs = 0
        self._output_dir: Path | None = None
        self._run_start_time: float = 0.0
        self._poll_timer: ui.timer | None = None
        self._multi_progress = None  # MultiRunProgress | None

        # UI refs
        self._campaign_select: ui.select | None = None
        self._filter_info: ui.label | None = None
        self._run_btn: ui.button | None = None
        self._conf_slider: ui.slider | None = None
        self._overlap_slider: ui.slider | None = None
        self._locales_select: ui.select
        self._progress_col: ui.column | None = None
        self._progress_bar: ui.linear_progress | None = None
        self._progress_label: ui.label | None = None
        self._eta_label: ui.label | None = None
        self._results_col: ui.column | None = None

    def build(self) -> None:
        ui.label('BirdNET').classes('text-h5')

        # Campaign selector
        with ui.row().classes('items-center gap-4 mb-2'):
            self._campaign_select = ui.select(
                options={},
                label='Campaign',
                on_change=lambda e: self._on_campaign_change(e.value),
            ).classes('w-72').props('outlined dense options-dense')
            self._filter_info = ui.label('').classes('text-caption text-grey self-center')

        ui.separator().classes('my-3')

        # Analysis settings, all controls in one row
        with ui.row().classes('items-start gap-8'):
            # Min confidence
            with ui.column().classes('gap-1'):
                ui.label('Min confidence').classes('text-caption font-medium')
                with ui.row().classes('items-center gap-3'):
                    self._conf_slider = ui.slider(min=0.10, max=1.0, step=0.01).bind_value(project_settings, 'birdnet_min_conf').on('update:model-value', lambda _: self._save_settings()).classes('w-40')
                    ui.label().bind_text_from(project_settings, 'birdnet_min_conf', lambda v: f'{v:.2f}').classes('w-8 text-caption')

            # Overlap
            with ui.column().classes('gap-1'):
                ui.label('Overlap (s)').classes('text-caption font-medium')
                with ui.row().classes('items-center gap-3'):
                    self._overlap_slider = ui.slider(min=0.0, max=2.9, step=0.1).bind_value(project_settings, 'birdnet_overlap').on('update:model-value', lambda _: self._save_settings()).classes('w-40')
                    ui.label().bind_text_from(project_settings, 'birdnet_overlap', lambda v: f'{v:.1f}').classes('w-8 text-caption')

            # Additional locales ("en" is always available as BirdNET's internal US English)
            with ui.column().classes('gap-1'):
                ui.label('Additional language columns for the species').classes('text-caption font-medium')
                available_locales = _get_available_locales()
                self._locales_select = ui.select(
                    options=sorted({'en', *available_locales}),
                    multiple=True,
                    label='Locales',
                ).bind_value(project_settings, 'birdnet_locales').on('update:model-value', lambda _: self._save_settings()).props('outlined dense use-chips').classes('w-52')
                if not available_locales:
                    ui.label('No BirdNET language label files found, only English available').classes('text-caption text-grey')

        ui.separator().classes('my-3')

        # Run button
        self._run_btn = ui.button(
            'Run BirdNET',
            icon='flutter_dash',
            on_click=self._on_run,
        ).props('color=primary')
        self._run_btn.set_enabled(False)

        # Progress
        with ui.column().classes('gap-1 mt-4 w-96') as self._progress_col:
            with ui.row().classes('items-center justify-between w-full'):
                self._progress_label = ui.label('Starting...').classes('text-caption text-grey')
                self._eta_label = ui.label('').classes('text-caption text-grey')
            self._progress_bar = ui.linear_progress(value=0, show_value=False).props('rounded color=primary')
        self._progress_col.set_visibility(False)

        # Results
        self._results_col = ui.column().classes('gap-0 mt-3')
        self._results_col.set_visibility(False)

        self.refresh_campaigns()

    # Campaign management:
    # - Discover campaigns in the recordings directory
    # - populate the campaign selector
    # - update filter info (location or species list) when the selection changes
    # - enable the Run button
    def refresh_campaigns(self) -> None:
        self._campaign_paths = discover_campaigns(Path(project_settings.audio_recordings_path))
        n = len(self._campaign_paths)
        options = {_ALL_CAMPAIGNS: f'All campaigns ({n})' if n else 'All campaigns'} | {name: name for name in self._campaign_paths}
        self._campaign_select.options = options
        self._campaign_select.set_value(_ALL_CAMPAIGNS)
        self._campaign_select.update()
        self._on_campaign_change(_ALL_CAMPAIGNS)

    def _on_campaign_change(self, value) -> None:
        if value == _ALL_CAMPAIGNS:
            self._campaign_dir = None
            self._campaign_settings = None
            n = len(self._campaign_paths)
            self._filter_info.set_text(f'{n} campaign{"s" if n != 1 else ""}' if n else 'No campaigns found')
        elif value and value in self._campaign_paths:
            self._campaign_dir = self._campaign_paths[value]
            try:
                self._campaign_settings = CampaignSettings.load(self._campaign_dir)
                self._update_filter_info()
            except Exception:
                self._campaign_settings = None
                self._filter_info.set_text('Could not load campaign.toml')
        else:
            self._campaign_dir = None
            self._campaign_settings = None
            self._filter_info.set_text('')

        self._run_btn.set_enabled(self._can_run and not self._running)

    def _update_filter_info(self) -> None:
        cs = self._campaign_settings
        if cs is None:
            return
        if cs.species_filter_mode == 'location':
            lat_d = 'N' if cs.latitude >= 0 else 'S'
            lon_d = 'E' if cs.longitude >= 0 else 'W'
            self._filter_info.set_text(f'● Location  {abs(cs.latitude):.2f}°{lat_d}, {abs(cs.longitude):.2f}°{lon_d}')
        else:
            self._filter_info.set_text('● Species list')

    @property
    def _can_run(self) -> bool:
        return (self._campaign_select.value == _ALL_CAMPAIGNS and bool(self._campaign_paths)) or (self._campaign_dir is not None and self._campaign_settings is not None)

    def _notify_error(self, exc) -> None:
        try:
            ui.notify(f'Analysis failed: {exc}', type='negative', timeout=0)
        except Exception:
            print(f'Analysis failed: {exc}')

    def _save_settings(self) -> None:
        path = get_project_path()
        if path:
            project_settings.save(path)

    def _build_settings(self):
        return AnalysisSettings(
            min_conf=project_settings.birdnet_min_conf,
            overlap=project_settings.birdnet_overlap,
            locales=list(project_settings.birdnet_locales),
        )

    def _set_running(self, running: bool) -> None:
        self._running = running
        enabled = not running
        self._run_btn.set_enabled(enabled and self._can_run)
        self._campaign_select.set_enabled(enabled)
        self._conf_slider.set_enabled(enabled)
        self._overlap_slider.set_enabled(enabled)
        self._locales_select.set_enabled(enabled)

    # Run:
    # - Dispatch to _run_single or _run_all based on the campaign selector
    # - Collect WAV counts before starting
    # - Kick off the BirdNET subprocess via run.io_bound
    # - Hand results to _show_results when done
    async def _on_run(self) -> None:
        if self._running:
            return
        if self._campaign_select.value == _ALL_CAMPAIGNS:
            await self._run_all()
        else:
            await self._run_single()

    async def _run_single(self) -> None:
        if not self._campaign_dir or not self._campaign_settings:
            return

        from pam_analyzer.core.birdnet_runner import run_analysis

        self._output_dir = get_project_output_base() / self._campaign_dir.name
        self._multi_progress = None

        campaign_dir = self._campaign_dir
        campaign_settings = self._campaign_settings
        output_dir = self._output_dir
        settings = self._build_settings()

        self._total_wavs = await run.io_bound(count_wav_files, campaign_dir)

        self._start_progress(f'0 / {self._total_wavs} files')

        result = None
        try:
            result = await run.io_bound(
                run_analysis,
                campaign_dir,
                campaign_settings,
                output_dir,
                settings,
                project_settings.preferred_species_lang,
                Path(project_settings.audio_recordings_path),
            )
        except Exception as exc:
            self._notify_error(exc)
        finally:
            self._stop_progress()

        if result is not None:
            self._show_results([result], output_dir)

    async def _run_all(self) -> None:
        from pam_analyzer.core.birdnet_runner import MultiRunProgress, run_all_campaigns

        campaigns: dict[str, tuple[Path, CampaignSettings]] = {}
        for name, path in self._campaign_paths.items():
            try:
                campaigns[name] = (path, CampaignSettings.load(path))
            except Exception:
                pass

        if not campaigns:
            ui.notify('No campaigns found', type='warning')
            return

        self._output_dir = get_project_output_base()
        self._multi_progress = MultiRunProgress()

        output_dir = self._output_dir
        settings = self._build_settings()

        self._total_wavs = await run.io_bound(lambda: sum(count_wav_files(camp_dir) for camp_dir, _ in campaigns.values()))

        self._start_progress(f'0 / {len(campaigns)} campaigns · 0 / {self._total_wavs} files')

        project_name = get_project_name(get_project_path()) or 'project'

        run_result = None
        try:
            run_result = await run.io_bound(
                run_all_campaigns,
                campaigns,
                output_dir,
                settings,
                project_name,
                self._multi_progress,
                project_settings.preferred_species_lang,
                Path(project_settings.audio_recordings_path),
            )
        except Exception as exc:
            self._notify_error(exc)
        finally:
            self._stop_progress()

        if run_result is not None:
            self._show_results(
                run_result.results,
                output_dir,
                combined_csv=run_result.combined_csv,
                per_campaign_aru_csv=run_result.per_campaign_aru_csv,
                all_campaigns_csv=run_result.all_campaigns_csv,
            )

    # Progress & results:
    # - _start/_stop_progress show/hide the progress bar and manage a polling timer
    # - _poll_progress counts freshly written CSV files to track completion and compute ETA
    # - _show_results builds the post-run summary UI with detection counts, elapsed time, and CSV buttons
    def _start_progress(self, label: str) -> None:
        self._set_running(True)
        self._results_col.set_visibility(False)
        self._progress_bar.set_value(0)
        self._eta_label.set_text('')
        self._progress_label.set_text(label)
        self._progress_col.set_visibility(True)
        self._run_start_time = time.time()
        self._poll_timer = ui.timer(2.0, self._poll_progress)

    def _stop_progress(self) -> None:
        if self._poll_timer:
            self._poll_timer.cancel()
            self._poll_timer = None
        try:
            self._set_running(False)
            self._progress_col.set_visibility(False)
        except RuntimeError:
            pass  # client was closed while analysis was running

    def _poll_progress(self) -> None:
        if not self._output_dir or not self._output_dir.exists():
            return
        done = sum(1 for p in self._output_dir.rglob('*.BirdNET.results.csv') if p.stat().st_mtime >= self._run_start_time)
        total = self._total_wavs
        fraction = (done / total) if total else 0.0
        try:
            self._progress_bar.set_value(fraction)
            elapsed = time.time() - self._run_start_time
            self._eta_label.set_text(format_eta(done, total, elapsed))
            if self._multi_progress and self._multi_progress.total_campaigns:
                p = self._multi_progress
                self._progress_label.set_text(f'Campaign {p.campaign_index}/{p.total_campaigns}: {p.current_campaign}  ·  {done} / {total} files')
            else:
                self._progress_label.set_text(f'{done} / {total} files')
        except RuntimeError:
            pass  # client was closed while analysis was running

    def _show_results(
        self,
        results: list,
        output_dir: Path,
        *,
        combined_csv: Path | None = None,
        per_campaign_aru_csv: Path | None = None,
        all_campaigns_csv: Path | None = None,
    ) -> None:
        try:
            self._results_col.clear()
        except RuntimeError:
            return  # client was closed while analysis was running

        for result in results:
            for warning in result.warnings:
                ui.notify(warning, type='warning', timeout=10000)

        total_detections = sum(r.detection_count for r in results)
        total_wavs = sum(r.wav_count for r in results)
        total_arus = sum(r.aru_count for r in results)
        total_weeks = sum(len(r.week_results) for r in results)
        total_elapsed = sum(r.elapsed for r in results)
        mins, secs = divmod(int(total_elapsed), 60)
        elapsed_str = f'{mins}m {secs:02d}s' if mins else f'{secs}s'

        parts = [f'{total_detections:,} detections']
        if len(results) > 1:
            parts.append(f'{len(results)} campaigns')
        if total_arus:
            parts.append(f'{total_arus} ARUs')
        if total_weeks:
            parts.append(f'{total_weeks} weeks')
        parts.append(f'{total_wavs} WAV files')
        parts.append(elapsed_str)
        summary = '✓ ' + ' · '.join(parts)

        self._results_col.set_visibility(True)

        def _csv_buttons(paths: list[Path]) -> None:
            for path in paths:
                if path.exists():
                    ui.button(
                        path.name,
                        icon='table_chart' if path.suffix == '.csv' else 'list',
                        on_click=lambda p=path: open_file(str(p)),
                    ).props('flat size=sm no-caps')

        with self._results_col:
            ui.label(summary).classes('text-positive font-medium')
            ui.separator().classes('my-3')
            with ui.row().classes('items-center gap-1 mb-1'):
                ui.label('CSVs written to:').classes('text-caption text-grey')
                ui.label(contract_user_path(str(output_dir))).classes('text-blue-500 cursor-pointer font-mono text-caption').on('click', lambda o=output_dir: open_native_file_manager(str(o)))

            # Project-level CSVs (all-campaigns run)
            project_csvs = [p for p in [combined_csv, per_campaign_aru_csv, all_campaigns_csv] if p is not None]
            if project_csvs:
                with ui.row().classes('items-center gap-1 flex-wrap'):
                    ui.label(get_project_name(get_project_path()) or 'project').classes('text-caption font-medium w-40 shrink-0')
                    ui.button(
                        icon='folder_open',
                        on_click=lambda o=output_dir: open_native_file_manager(str(o)),
                    ).props('flat size=xs').tooltip('Open folder')
                    _csv_buttons(project_csvs)

            # Per-campaign rows
            for r in results:
                campaign_name = r.detections_csv.stem.removesuffix('-detections')

                def _campaign_header(r=r, campaign_name=campaign_name) -> None:
                    ui.label(campaign_name).classes('text-caption font-medium w-40 shrink-0')
                    ui.button(
                        icon='folder_open',
                        on_click=lambda o=r.output_dir: open_native_file_manager(str(o)),
                    ).props('flat size=xs').tooltip('Open folder')
                    _csv_buttons(
                        [
                            r.detections_csv,
                            r.per_aru_csv,
                            r.all_arus_csv,
                            r.output_dir / f'{campaign_name}-species-list.txt',
                        ]
                    )

                if r.week_results:
                    with ui.expansion().classes('w-full').props('dense content-style="padding: 0"') as exp:
                        with exp.add_slot('header'):
                            with ui.row().classes('items-center gap-1 flex-wrap w-full'):
                                _campaign_header()
                        for wr in r.week_results:
                            with ui.row().classes('items-center gap-1 flex-wrap'):
                                with ui.element('div').classes('w-40 shrink-0 flex justify-end pr-2'):
                                    ui.label(f'Week {wr.week}').classes('text-caption text-grey')
                                ui.button(
                                    icon='folder_open',
                                    on_click=lambda o=wr.detections_csv.parent: open_native_file_manager(str(o)),
                                ).props('flat size=xs').tooltip('Open folder')
                                _csv_buttons(
                                    [
                                        wr.detections_csv,
                                        wr.per_aru_csv,
                                        wr.all_arus_csv,
                                        wr.species_list_txt,
                                    ]
                                )
                else:
                    with ui.expansion().classes('w-full').props('dense content-style="padding: 0" hide-expand-icon') as _exp:
                        with _exp.add_slot('header'):
                            with ui.row().classes('items-center gap-1 flex-wrap w-full'):
                                _campaign_header()
                                if r.wav_count == 0:
                                    ui.label('No audio files').classes('text-caption text-grey italic')
