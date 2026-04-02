"""Detection CSV I/O and audio snippet helpers."""

import csv
import re
from datetime import datetime
from pathlib import Path

import soundfile as sf

# Annotation columns written by the user in the Examine panel
ANNOTATION_FIELDS = ['Verified', 'Corrected_Species', 'Comment']

# Columns that hold numeric values (used for parsing and AG Grid filter type)
NUMERIC_FIELDS = {'Start_Time', 'End_Time', 'Confidence', 'Week', 'Rank'}

_VERIFIED_SUFFIX = {
    'true': '_confirmed',
    'false': '_incorrect',
    'uncertain': '_uncertain',
}


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    """Write rows to a CSV file, ignoring extra fields."""
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def read_csv(csv_path: Path) -> tuple[list[dict], list[str]]:
    """Read a detections CSV, returning (rows, fieldnames).

    Numeric fields are converted to floats for proper grid sorting/filtering.
    """
    with open(csv_path, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = [parse_numeric(row) for row in reader]
    return rows, list(fieldnames)


def build_snippet_filename(
    row: dict,
    play_start: float,
    play_end: float,
    corrected_sci_map: dict[str, str] | None = None,
) -> str:
    """Build an encoded filename for an audio snippet.

    If ``Corrected_Species`` is set, the corrected name (and its scientific name
    looked up via ``corrected_sci_map``) replaces the original species, and
    ``_corrected`` is appended before the ``.wav``.
    If ``Verified`` is set, ``_confirmed``, ``_incorrect``, or ``_uncertain``
    is appended before the ``.wav`` depending on the value.
    """
    campaign = row.get('Campaign', 'unknown')
    aru = row.get('ARU', 'unknown')
    scientific = row.get('Scientific_Name', '')
    species = row.get('Species', '')
    rank = row.get('Rank', '')
    confidence = row.get('Confidence', '')
    rec_time = row.get('Recording_Time', '')
    verified = row.get('Verified', '')
    corrected_species = row.get('Corrected_Species', '')

    try:
        time_str = datetime.fromisoformat(str(rec_time)).strftime('%Y%m%d_%H%M%S')
    except (ValueError, TypeError):
        time_str = str(rec_time) or 'unknown_time'

    try:
        conf_str = f'conf{float(confidence):.4f}'
    except (ValueError, TypeError):
        conf_str = f'conf{confidence}'

    sci_name = (corrected_sci_map or {}).get(corrected_species) or scientific if corrected_species else scientific
    common_name = corrected_species or species
    species_part = '_'.join(filter(None, [sci_name, common_name])) or 'unknown_species'

    suffix = ('_corrected' if corrected_species else '') + _VERIFIED_SUFFIX.get(verified, '')

    name = (
        '_-_'.join(
            [
                campaign,
                aru,
                species_part,
                f'segrank{rank}',
                conf_str,
                time_str,
                str(play_start),
                str(play_end),
            ]
        )
        + suffix
    )
    return re.sub(r'[<>:"/\\|?*]', '_', name) + '.wav'


_duration_cache: dict[Path, float] = {}


def audio_duration(path: Path) -> float:
    """Return WAV duration in seconds by reading just the file header. Cached by path."""
    if path not in _duration_cache:
        try:
            _duration_cache[path] = sf.info(path).duration
        except Exception:
            _duration_cache[path] = 0.0
    return _duration_cache[path]


def extract_audio_snippet(src: Path, start: float, end: float, dst: Path) -> None:
    """Extract an audio segment using soundfile."""
    audio, sr = sf.read(src)
    end = min(len(audio) / sr, end)
    snippet = audio[int(start * sr) : int(end * sr)]
    sf.write(dst, snippet, sr)


def parse_numeric(row: dict) -> dict:
    """Convert numeric string fields to actual numbers for proper sorting/filtering."""
    for field in NUMERIC_FIELDS:
        if field in row and row[field]:
            try:
                row[field] = float(row[field])
            except (ValueError, TypeError):
                pass
    return row
