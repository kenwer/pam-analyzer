from __future__ import annotations

import csv
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import birdnet_analyzer
import birdnet_analyzer.config as birdnet_cfg

from pam_analyzer.core.campaign_settings import SPECIES_LIST_FILENAME, CampaignSettings


# Shared with the UI to show live progress during a run_all_campaigns() call.
# The UI polls this object on a timer to update the progress label.
@dataclass
class MultiRunProgress:
    campaign_index: int = 0
    total_campaigns: int = 0
    current_campaign: str = ''


# User-facing analysis parameters, built from the BirdNET panel controls.
# Passed to run_analysis() and run_all_campaigns().
@dataclass
class AnalysisSettings:
    min_conf: float = 0.25
    overlap: float = 0.0
    locales: list[str] = field(default_factory=list)


# One week's worth of output CSVs within a single campaign.
# Produced by _write_week_tables() when the campaign uses week_NN directories.
@dataclass
class WeekResult:
    week: int
    detections_csv: Path
    per_aru_csv: Path
    all_arus_csv: Path
    species_list_txt: Path


# Returned by run_analysis() for a single campaign.
# Contains paths to all output CSVs and summary stats for the UI to display.
@dataclass
class AnalysisResult:
    output_dir: Path
    detections_csv: Path
    per_aru_csv: Path
    all_arus_csv: Path
    week_results: list[WeekResult]
    detection_count: int
    wav_count: int
    aru_count: int
    elapsed: float
    warnings: list[str] = field(default_factory=list)


# Returned by run_all_campaigns() when running every campaign at once.
# Wraps per-campaign AnalysisResults plus project-level combined CSVs.
@dataclass
class AllCampaignsResult:
    results: list[AnalysisResult]
    combined_csv: Path
    per_campaign_aru_csv: Path
    all_campaigns_csv: Path


_AUDIO_EXTENSIONS = {'.wav', '.flac', '.mp3', '.ogg', '.m4a', '.wma', '.aiff', '.aif'}  # mirrors birdnet_analyzer ALLOWED_FILETYPES


def count_wav_files(campaign_dir: Path) -> int:
    """Count all BirdNET-supported audio files recursively under campaign_dir."""
    return sum(1 for f in campaign_dir.rglob('*') if f.is_file() and f.suffix.lower() in _AUDIO_EXTENSIONS)


def _week_from_path(path: Path) -> int | None:
    """Extract BirdNET week number from a week_NN directory component, if present."""
    for part in path.parts:
        if part.startswith('week_'):
            try:
                return int(part.split('_', 1)[1])
            except (IndexError, ValueError):
                pass
    return None


def _parse_recording_time(stem: str) -> datetime | None:
    match = re.search(r'(\d{8}_\d{6})', stem)
    if match:
        try:
            return datetime.strptime(match.group(1), '%Y%m%d_%H%M%S')
        except ValueError:
            pass
    return None


@lru_cache(maxsize=1)
def _locale_file_map() -> dict[str, Path]:
    """Return {locale_code: path} for all label files in the installed birdnet_analyzer."""
    labels_dir = Path(birdnet_analyzer.__file__).parent / 'labels' / 'V2.4'
    prefix = 'BirdNET_GLOBAL_6K_V2.4_Labels_'
    return {
        p.stem[len(prefix):]: p
        for p in sorted(labels_dir.glob(f'{prefix}*.txt'))
    }


def get_available_locales() -> list[str]:
    """Return locale codes available in the installed birdnet_analyzer."""
    return list(_locale_file_map())


def get_species_options(output_dir: Path, campaign_name: str, locale: str) -> list[str]:
    """Return sorted common names (in locale) from all species-list-*.txt files for a campaign.

    Lines in those files are "Scientific name_English common name". If a locale label file
    exists the scientific name is mapped to the localised name; otherwise the English name
    from the file is used as the fallback.
    """
    patterns = [
        f'{campaign_name}-species-list-week-*.txt',
        f'{campaign_name}-species-list.txt',
    ]
    paths: list[Path] = []
    for pat in patterns:
        paths.extend(output_dir.glob(pat))
    if not paths:
        return []

    label_map = _load_locale_labels(locale)
    names: set[str] = set()
    for path in paths:
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if '_' not in line:
                continue
            sci, english = line.split('_', 1)
            name = label_map.get(sci) or english
            if name:
                names.add(name)
    return sorted(names)


@lru_cache(maxsize=3)
def _load_locale_labels(locale: str) -> dict[str, str]:
    """Return scientific_name -> localized_common_name mapping for a locale."""
    loc_path = _locale_file_map().get(locale)
    if not loc_path:
        return {}
    mapping: dict[str, str] = {}
    with open(loc_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if '_' in line:
                sci, localized = line.split('_', 1)
                mapping[sci] = localized
    return mapping


def build_locale_reverse_map(locale: str) -> dict[str, str]:
    """Return localized_common_name -> scientific_name mapping for a locale."""
    return {v: k for k, v in _load_locale_labels(locale).items()}


def _parse_result_csv(
    result_csv: Path,
    campaign_name: str,
    campaign_dir: Path,
    audio_root: Path,
    week: int,
    settings: AnalysisSettings,
    locale_maps: dict[str, dict[str, str]],
    run_context: dict,
    preferred_lang_map: dict[str, str] | None = None,
) -> list[dict]:
    """Parse one BirdNET result CSV into normalised detection dicts."""
    # BirdNET on Windows may write paths with system encoding (e.g. cp1252) rather than UTF-8.
    # latin-1 decodes any byte sequence without error and correctly maps 0x80-0xFF to Unicode.
    try:
        with open(result_csv, newline='', encoding='utf-8') as f:
            file_rows = list(csv.DictReader(f))
    except UnicodeDecodeError:
        with open(result_csv, newline='', encoding='latin-1') as f:
            file_rows = list(csv.DictReader(f))

    # Rank each detection within its segment by descending confidence
    seg_groups: dict[tuple, list] = defaultdict(list)
    for row in file_rows:
        seg_groups[(row['Start (s)'], row['End (s)'])].append(row)
    seg_species_rank: dict[tuple, int] = {}  # Stores the rank of each species within its time segment
    for seg_rows in seg_groups.values():
        for rank, seg_row in enumerate(
            sorted(seg_rows, key=lambda r: float(r['Confidence']), reverse=True),
            start=1,
        ):
            seg_species_rank[(seg_row['Start (s)'], seg_row['End (s)'], seg_row['Common name'])] = rank

    detections = []
    for row in file_rows:
        if float(row['Confidence']) < settings.min_conf:
            continue
        rank = seg_species_rank.get((row['Start (s)'], row['End (s)'], row['Common name']), 0)
        scientific_name = row['Scientific name']
        file_path = Path(row['File'])
        try:
            aru_number = file_path.relative_to(campaign_dir).parts[0]
        except ValueError:
            aru_number = file_path.parts[-3] if len(file_path.parts) >= 3 else ''

        recording_time = _parse_recording_time(file_path.stem)
        # "en" is a virtual locale meaning BirdNET's native US English common name;
        # there is no en label file (only en_uk), so we use row["Common name"] directly.
        locale_names = {f'Species_{loc}': (row['Common name'] if loc == 'en' else locale_maps[loc].get(scientific_name, '')) for loc in settings.locales}
        species_name = preferred_lang_map.get(scientific_name) or row['Common name'] if preferred_lang_map else row['Common name']
        detections.append(
            {
                'Campaign': campaign_name,
                'ARU': aru_number,
                'Start_Time': row['Start (s)'],
                'End_Time': row['End (s)'],
                'Scientific_Name': scientific_name,
                'Species': species_name,
                **locale_names,
                'Confidence': row['Confidence'],
                'Rank': rank,
                'File': Path(row['File']).relative_to(audio_root).as_posix(),
                'Recording_Time': str(recording_time) if recording_time else '',
                'Week': _week_from_path(result_csv) or week,
                **run_context,
                'Verified': '',
                'Corrected_Species': '',
                'Comment': '',
            }
        )
    return detections


def _write_summary_tables(
    detections: list[dict],
    output_dir: Path,
    locale_cols: list[str],
    campaign_name: str,
    file_prefix: str | None = None,
) -> tuple[Path, Path]:
    """Write {prefix}-summary-per-aru.csv and {prefix}-summary-all-arus.csv.

    file_prefix overrides the filename prefix while campaign_name is still used
    for the campaign column value.
    """
    prefix = file_prefix or campaign_name

    per_aru: dict[tuple, dict] = defaultdict(
        lambda: {
            'count': 0,
            'max_conf': 0.0,
            'best_rank': float('inf'),
            'scientific_name': '',
            'locale_names': {},
        }
    )
    for row in detections:
        key = (row['ARU'], row['Species'])
        per_aru[key]['count'] += 1
        per_aru[key]['max_conf'] = max(per_aru[key]['max_conf'], float(row['Confidence']))
        per_aru[key]['best_rank'] = min(per_aru[key]['best_rank'], int(row['Rank']))
        per_aru[key]['scientific_name'] = row['Scientific_Name']
        for col in locale_cols:
            per_aru[key]['locale_names'][col] = row.get(col, '')

    per_aru_path = output_dir / f'{prefix}-summary-per-aru.csv'
    with open(per_aru_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                'Campaign',
                'ARU',
                'Scientific_Name',
                'Species',
                *locale_cols,
                'detection_count',
                'max_confidence',
                'best_species_rank',
            ],
        )
        writer.writeheader()
        for (aru, species_name), data in sorted(per_aru.items(), key=lambda x: (x[0][0], -x[1]['count'])):
            writer.writerow(
                {
                    'Campaign': campaign_name,
                    'ARU': aru,
                    'Species': species_name,
                    'Scientific_Name': data['scientific_name'],
                    **data['locale_names'],
                    'detection_count': data['count'],
                    'max_confidence': f'{data["max_conf"]:.4f}',
                    'best_species_rank': f'top-{int(data["best_rank"])}',
                }
            )

    global_agg: dict[str, dict] = defaultdict(
        lambda: {
            'count': 0,
            'max_conf': 0.0,
            'arus': set(),
            'scientific_name': '',
            'best_rank': float('inf'),
            'locale_names': {},
        }
    )
    for (aru, species_name), data in per_aru.items():
        global_agg[species_name]['count'] += data['count']
        global_agg[species_name]['max_conf'] = max(global_agg[species_name]['max_conf'], data['max_conf'])
        global_agg[species_name]['arus'].add(aru)
        global_agg[species_name]['scientific_name'] = data['scientific_name']
        global_agg[species_name]['best_rank'] = min(global_agg[species_name]['best_rank'], data['best_rank'])
        global_agg[species_name]['locale_names'].update(data['locale_names'])

    all_arus_path = output_dir / f'{prefix}-summary-all-arus.csv'
    with open(all_arus_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                'Campaign',
                'Scientific_Name',
                'Species',
                *locale_cols,
                'detection_count',
                'aru_count',
                'max_confidence',
                'best_species_rank_any_aru',
            ],
        )
        writer.writeheader()
        for species_name, data in sorted(global_agg.items(), key=lambda x: -x[1]['count']):
            writer.writerow(
                {
                    'Campaign': campaign_name,
                    'Species': species_name,
                    'Scientific_Name': data['scientific_name'],
                    **data['locale_names'],
                    'detection_count': data['count'],
                    'aru_count': len(data['arus']),
                    'max_confidence': f'{data["max_conf"]:.4f}',
                    'best_species_rank_any_aru': f'top-{int(data["best_rank"])}',
                }
            )

    return per_aru_path, all_arus_path


def _write_week_tables(
    detections: list[dict],
    output_dir: Path,
    fieldnames: list[str],
    locale_cols: list[str],
    campaign_name: str,
) -> list[WeekResult]:
    """Write per-week detections and summary CSVs; return a WeekResult per week."""
    by_week: dict[int, list[dict]] = defaultdict(list)
    for d in detections:
        w = d.get('Week')
        if w not in (None, -1):
            by_week[int(w)].append(d)

    results = []
    for week_num in sorted(by_week):
        week_dets = by_week[week_num]
        prefix = f'{campaign_name}-week-{week_num:02d}'

        det_csv = output_dir / f'{prefix}-detections.csv'
        with open(det_csv, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(week_dets)

        per_aru_csv, all_arus_csv = _write_summary_tables(week_dets, output_dir, locale_cols, campaign_name, file_prefix=prefix)
        species_txt = output_dir / f'{campaign_name}-species-list-week-{week_num:02d}.txt'
        results.append(
            WeekResult(
                week=week_num,
                detections_csv=det_csv,
                per_aru_csv=per_aru_csv,
                all_arus_csv=all_arus_csv,
                species_list_txt=species_txt,
            )
        )
    return results


def _prewarm_model() -> None:
    """Trigger birdnet_analyzer model initialisation before multi-threaded analysis.

    birdnet_analyzer extracts its model zip on first use. If multiple threads hit
    this simultaneously (as happens with threads > 1), the extraction can race and
    produce a FileNotFoundError. Calling this once, single-threaded, avoids that.
    """
    try:
        from birdnet_analyzer.model import ensure_model_exists  # type: ignore[import]

        ensure_model_exists()
    except Exception:
        pass  # best-effort, let the real analysis surface any real errors


def run_analysis(
    campaign_dir: Path,
    campaign_settings: CampaignSettings,
    output_dir: Path,
    settings: AnalysisSettings,
    preferred_lang: str = 'en',
    audio_root: Path | None = None,
) -> AnalysisResult:
    """Run BirdNET analysis on campaign_dir. Blocking, call via run.io_bound()."""
    _prewarm_model()
    t0 = time.monotonic()
    campaign_name = campaign_dir.name
    output_dir.mkdir(parents=True, exist_ok=True)
    _audio_root = audio_root if audio_root is not None else campaign_dir.parent

    if campaign_settings.species_filter_mode == 'location':
        lat: float | None = campaign_settings.latitude
        lon: float | None = campaign_settings.longitude
        slist = None
        week_dirs = sorted(d for d in campaign_dir.rglob('week_*') if d.is_dir())
        week = -1  # resolved per-dir below; year-round if no week dirs found
    else:
        lat, lon = None, None
        week_dirs = []
        week = -1
        species_list_file = campaign_dir / SPECIES_LIST_FILENAME
        slist = str(species_list_file) if species_list_file.exists() else None

    num_threads = os.cpu_count() or 8
    analyze_kwargs = {
        'min_conf': settings.min_conf,
        'slist': slist,  # BirdNET only uses the species list if no lat/lon are given
        'lat': lat if lat is not None else -1,  # BirdNET uses -1 to mean "no location, use slist"
        'lon': lon if lon is not None else -1,
        'overlap': settings.overlap,
        'top_n': None,  # BirdNET treats top_n and min_conf as mutually exclusive: we set it None so that min_conf is never bypassed
        'rtype': 'csv',
        'combine_results': False,
        'threads': num_threads,
        'locale': 'en',
    }

    if week_dirs:
        for week_dir in week_dirs:
            dir_week = _week_from_path(week_dir)
            if dir_week is None:
                continue
            week_out = output_dir / week_dir.relative_to(campaign_dir)
            week_out.mkdir(parents=True, exist_ok=True)
            birdnet_analyzer.analyze(str(week_dir), output=str(week_out), week=dir_week, **analyze_kwargs)
    else:
        birdnet_analyzer.analyze(str(campaign_dir), output=str(output_dir), week=week, **analyze_kwargs)

    result_csvs = sorted(output_dir.rglob('*.BirdNET.results.csv'))
    wav_count = count_wav_files(campaign_dir)

    detections_csv = output_dir / f'{campaign_name}-detections.csv'
    per_aru_csv = output_dir / f'{campaign_name}-summary-per-aru.csv'
    all_arus_csv = output_dir / f'{campaign_name}-summary-all-arus.csv'

    if not result_csvs:
        return AnalysisResult(
            output_dir=output_dir,
            detections_csv=detections_csv,
            per_aru_csv=per_aru_csv,
            all_arus_csv=all_arus_csv,
            week_results=[],
            detection_count=0,
            wav_count=wav_count,
            aru_count=0,
            elapsed=time.monotonic() - t0,
        )

    run_context = {
        'Lat': lat if lat is not None else '',
        'Lon': lon if lon is not None else '',
        'Species_List': slist or '',
        'Min_Conf': settings.min_conf,
        'Model': Path(birdnet_cfg.MODEL_PATH).name if birdnet_cfg.MODEL_PATH else '',
    }

    preferred_lang_map = _load_locale_labels(preferred_lang)
    locale_maps = {loc: _load_locale_labels(loc) for loc in settings.locales if loc != 'en'}  # Build a locale map but filter out "en" as that's always {} (no file) because it's BirdNET internal lang
    locale_cols = [f'Species_{loc}' for loc in settings.locales]

    fieldnames = [
        'Campaign',
        'ARU',
        'Start_Time',
        'End_Time',
        'Scientific_Name',
        'Species',
        *locale_cols,
        'Confidence',
        'Rank',
        'File',
        'Recording_Time',
        'Week',
        *run_context.keys(),
        'Verified',
        'Corrected_Species',
        'Comment',
    ]

    detections: list[dict] = []

    with open(detections_csv, 'w', newline='', encoding='utf-8') as outfile:
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()
        for result_csv in result_csvs:
            rows = _parse_result_csv(
                result_csv,
                campaign_name,
                campaign_dir,
                _audio_root,
                week,
                settings,
                locale_maps,
                run_context,
                preferred_lang_map,
            )
            writer.writerows(rows)
            detections.extend(rows)

    per_aru_csv, all_arus_csv = _write_summary_tables(detections, output_dir, locale_cols, campaign_name)
    week_results = _write_week_tables(detections, output_dir, fieldnames, locale_cols, campaign_name)

    # Export geographic species list(s) in location mode
    warnings: list[str] = []
    if lat is not None and lon is not None:
        try:
            from birdnet_analyzer.species.utils import get_species_list

            if week_dirs:
                unique_weeks = sorted({w for d in week_dirs if (w := _week_from_path(d)) is not None})
                for w in unique_weeks:
                    geo_species = get_species_list(lat, lon, w, threshold=0.03)
                    (output_dir / f'{campaign_name}-species-list-week-{w:02d}.txt').write_text('\n'.join(geo_species) + '\n', encoding='utf-8')
            else:
                geo_species = get_species_list(lat, lon, week, threshold=0.03)
                (output_dir / f'{campaign_name}-species-list.txt').write_text('\n'.join(geo_species) + '\n', encoding='utf-8')
        except Exception as exc:
            msg = f'Failed to export geographic species list: {exc}'
            print(f'[birdnet] {msg}', file=sys.stderr)
            warnings.append(msg)

    return AnalysisResult(
        output_dir=output_dir,
        detections_csv=detections_csv,
        per_aru_csv=per_aru_csv,
        all_arus_csv=all_arus_csv,
        week_results=week_results,
        detection_count=len(detections),
        wav_count=wav_count,
        aru_count=len({row['ARU'] for row in detections}),
        elapsed=time.monotonic() - t0,
        warnings=warnings,
    )


def _write_combined_csv(
    named_results: list[tuple[str, AnalysisResult]],
    output_dir: Path,
    project_name: str,
) -> Path:
    """Concatenate per-campaign detection CSVs into {project_name}-detections.csv."""
    fieldnames: list[str] | None = None
    for _, result in named_results:
        if result.detections_csv.exists():
            with open(result.detections_csv, newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                if reader.fieldnames:
                    fieldnames = list(reader.fieldnames)
                    break

    combined_path = output_dir / f'{project_name}-detections.csv'
    if not fieldnames:
        return combined_path

    with open(combined_path, 'w', newline='', encoding='utf-8') as outfile:
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()
        for _, result in named_results:
            if not result.detections_csv.exists():
                continue
            with open(result.detections_csv, newline='', encoding='utf-8') as infile:
                for row in csv.DictReader(infile):
                    writer.writerow(row)

    return combined_path


def _write_project_summaries(
    named_results: list[tuple[str, AnalysisResult]],
    output_dir: Path,
    project_name: str,
    locale_cols: list[str],
) -> tuple[Path, Path]:
    """Write project-level summary CSVs from per-campaign per-ARU summaries.

    Produces:
      {project_name}-summary-per-campaign-aru.csv: concat of all per-campaign per-ARU rows
      {project_name}-summary-all-campaigns-all-arus.csv: one row per species, aggregated globally
    """
    # Collect all per-campaign per-ARU rows
    all_rows: list[dict] = []
    fieldnames: list[str] | None = None
    for _, result in named_results:
        if not result.per_aru_csv.exists():
            continue
        with open(result.per_aru_csv, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            if fieldnames is None and reader.fieldnames:
                fieldnames = list(reader.fieldnames)
            all_rows.extend(reader)

    per_campaign_aru_path = output_dir / f'{project_name}-summary-per-campaign-aru.csv'
    if fieldnames:
        with open(per_campaign_aru_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_rows)

    # Aggregate globally: one row per species across all campaigns and ARUs
    global_agg: dict[str, dict] = defaultdict(
        lambda: {
            'count': 0,
            'max_conf': 0.0,
            'campaigns': set(),
            'arus': set(),
            'scientific_name': '',
            'best_rank': float('inf'),
            'locale_names': {},
        }
    )
    for row in all_rows:
        species_name = row['Species']
        global_agg[species_name]['count'] += int(row['detection_count'])
        global_agg[species_name]['max_conf'] = max(global_agg[species_name]['max_conf'], float(row['max_confidence']))
        global_agg[species_name]['campaigns'].add(row['Campaign'])
        global_agg[species_name]['arus'].add(row['ARU'])
        global_agg[species_name]['scientific_name'] = row['Scientific_Name']
        rank_str = row.get('best_species_rank', 'top-99').replace('top-', '')
        try:
            global_agg[species_name]['best_rank'] = min(global_agg[species_name]['best_rank'], int(rank_str))
        except ValueError:
            pass
        for col in locale_cols:
            if row.get(col):
                global_agg[species_name]['locale_names'][col] = row[col]

    all_campaigns_path = output_dir / f'{project_name}-summary-all-campaigns-all-arus.csv'
    global_fieldnames = [
        'Scientific_Name',
        'Species',
        *locale_cols,
        'detection_count',
        'campaign_count',
        'aru_count',
        'max_confidence',
        'best_species_rank_any_campaign',
    ]
    with open(all_campaigns_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=global_fieldnames)
        writer.writeheader()
        for species_name, data in sorted(global_agg.items(), key=lambda x: -x[1]['count']):
            writer.writerow(
                {
                    'Species': species_name,
                    'Scientific_Name': data['scientific_name'],
                    **data['locale_names'],
                    'detection_count': data['count'],
                    'campaign_count': len(data['campaigns']),
                    'aru_count': len(data['arus']),
                    'max_confidence': f'{data["max_conf"]:.4f}',
                    'best_species_rank_any_campaign': f'top-{int(data["best_rank"])}',
                }
            )

    return per_campaign_aru_path, all_campaigns_path


def run_all_campaigns(
    campaigns: dict[str, tuple[Path, CampaignSettings]],
    output_dir: Path,
    settings: AnalysisSettings,
    project_name: str,
    progress: MultiRunProgress | None = None,
    preferred_lang: str = 'en',
    audio_root: Path | None = None,
) -> AllCampaignsResult:
    """Run BirdNET on every campaign sequentially. Blocking, call via run.io_bound()."""
    output_dir.mkdir(parents=True, exist_ok=True)

    if progress:
        progress.total_campaigns = len(campaigns)

    named_results: list[tuple[str, AnalysisResult]] = []
    for i, (name, (camp_dir, camp_settings)) in enumerate(campaigns.items()):
        if progress:
            progress.campaign_index = i + 1
            progress.current_campaign = name
        result = run_analysis(
            camp_dir,
            camp_settings,
            output_dir / name,
            settings,
            preferred_lang,
            audio_root,
        )
        named_results.append((name, result))

    locale_cols = [f'Species_{loc}' for loc in settings.locales]
    combined_csv = _write_combined_csv(named_results, output_dir, project_name)
    per_campaign_aru_csv, all_campaigns_csv = _write_project_summaries(named_results, output_dir, project_name, locale_cols)

    return AllCampaignsResult(
        results=[r for _, r in named_results],
        combined_csv=combined_csv,
        per_campaign_aru_csv=per_campaign_aru_csv,
        all_campaigns_csv=all_campaigns_csv,
    )
