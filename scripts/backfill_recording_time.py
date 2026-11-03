#!/usr/bin/env python3
"""Fill blank Recording_Time cells in a PAM Analyzer project's detection CSVs.

The filename timestamp parser used to match digits from the recorder serial
in names like 248D9B0164876836_20260520_075600.flac and left the time blank.
This re-derives it from each row's File column. Cells that already hold a
time are left alone, and a CSV without blank cells is not rewritten.

Stand-alone: needs only Python 3.8 or newer, no PAM Analyzer checkout.

    python backfill_recording_time.py /path/to/project --dry-run
    python backfill_recording_time.py /path/to/project

On Windows use double quotes around paths with spaces. Close the project in
PAM Analyzer first, or a later save from the app can overwrite the fixed
files with its in-memory copy.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

# Kept in step with pam_analyzer.domain.audio_import._RECORDING_TIME_RE.
_RECORDING_TIME_RE = re.compile(r"(?<!\d)(\d{8}_\d{6})(?!\d)")
_COLUMN = "Recording_Time"


def campaign_folders(project: Path) -> list[Path]:
    return sorted(d for d in project.iterdir() if d.is_dir() and (d / "campaign.toml").is_file())


def detection_csvs(campaign: Path) -> list[Path]:
    return sorted(
        p for p in campaign.iterdir()
        if p.is_file() and p.name.startswith("detections-") and p.name.endswith(".csv")
    )


def recording_time(file: str) -> str:
    """The analysis runner's str(datetime) form, or "" without a timestamp."""
    stem = PureWindowsPath(file).stem if "\\" in file else PurePosixPath(file).stem
    match = _RECORDING_TIME_RE.search(stem)
    if not match:
        return ""
    try:
        return str(datetime.strptime(match.group(1), "%Y%m%d_%H%M%S"))
    except ValueError:
        return ""


def backfill_csv(path: Path, dry_run: bool) -> tuple[int, int]:
    """Return (cells filled, cells still blank) for one CSV."""
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    if not rows or _COLUMN not in rows[0] or "File" not in rows[0]:
        return 0, 0
    header = rows[0]
    time_col, file_col = header.index(_COLUMN), header.index("File")

    filled = still_blank = 0
    for row in rows[1:]:
        if len(row) <= max(time_col, file_col) or row[time_col]:
            continue
        value = recording_time(row[file_col])
        if value:
            row[time_col] = value
            filled += 1
        else:
            still_blank += 1

    if filled and not dry_run:
        # Same dialect as the app's polars writer, so untouched rows keep their bytes.
        tmp = path.with_name(path.name + ".part")
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                csv.writer(f, lineterminator="\r\n").writerows(rows)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
    return filled, still_blank


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("project", type=Path, help="project folder (the one holding pam-analyzer.toml)")
    parser.add_argument("--dry-run", action="store_true", help="report counts without writing")
    args = parser.parse_args(argv)

    if not args.project.is_dir():
        print(f"Not a folder: {args.project}", file=sys.stderr)
        return 1
    campaigns = campaign_folders(args.project)
    if not campaigns:
        print(f"No campaign folders under {args.project}", file=sys.stderr)
        return 1

    total = 0
    for folder in campaigns:
        for path in detection_csvs(folder):
            filled, still_blank = backfill_csv(path, args.dry_run)
            total += filled
            note = f", {still_blank} without a timestamp in the filename" if still_blank else ""
            print(f"{folder.name}/{path.name}: filled {filled}{note}")

    verb = "Would fill" if args.dry_run else "Filled"
    print(f"{verb} {total} {_COLUMN} cells.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
