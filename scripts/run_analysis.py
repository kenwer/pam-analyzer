#!/usr/bin/env -S uv run python
"""Run BirdNET v3.0 over a project folder without a GUI.

Generates the same CSVs as the PAM Analyzer GUI app but headless on a server.
All configuration is taken from the project's TOML files: pam-analyzer.toml at
the project root (thresholds, overlap, locales, preferred language) and
one campaign.toml per campaign subfolder (species-filter mode and coordinates).

    uv run python scripts/run_analysis.py --project /path/to/project

By default every campaign that does not yet have a detections-<model>.csv is
analyzed. Pass --force to re-run and overwrite existing CSVs.

On a cold server the first run downloads model assets to the per-user cache. Set
BIRDNET_APP_DATA (read by the birdnet library) to redirect that cache if the
home directory is not writable.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from pam_analyzer.domain import (
    AnalysisProgressSnapshot,
    AnalysisRunResult,
    Campaign,
    Project,
    detection_schema,
    paths,
)
from pam_analyzer.domain.analysis import CancelledError
from pam_analyzer.infrastructure import BirdnetRunner

_log = logging.getLogger("run_model_on_project_folder")


class _ConsoleProgress:
    """AnalysisProgress that logs one line per phase change.

    is_cancelled() reports a flag flipped by a SIGINT handler so Ctrl-C asks the
    runner to stop at the next checkpoint. Cancelling mid session.run() can hang
    on some platforms (birdnet issue 51), so the handler stays minimal and never
    forces teardown itself.
    """

    def __init__(self) -> None:
        self._cancelled = False
        self._last_key: tuple[str, str] | None = None

    def request_cancel(self) -> None:
        self._cancelled = True

    def report(self, snapshot: AnalysisProgressSnapshot) -> None:
        key = (snapshot.campaign, snapshot.phase)
        if key == self._last_key:
            return
        self._last_key = key
        _log.info(
            "[%d/%d] %s: %s %d/%d",
            snapshot.campaign_index,
            snapshot.total_campaigns,
            snapshot.campaign,
            snapshot.phase,
            snapshot.files_done,
            snapshot.files_total,
        )

    def is_cancelled(self) -> bool:
        return self._cancelled


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run BirdNET v3.0 over a project folder headless.",
    )
    parser.add_argument(
        "--project",
        type=Path,
        required=True,
        help="Project folder containing pam-analyzer.toml and campaign subfolders.",
    )
    parser.add_argument(
        "--campaign",
        action="append",
        default=None,
        metavar="NAME",
        help="Restrict the run to this campaign (repeatable). Default: all campaigns.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run and overwrite campaigns that already have a detections CSV.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("PAM_LOG_LEVEL", "INFO"),
        help="Logging level (default: PAM_LOG_LEVEL env or INFO).",
    )
    return parser.parse_args(argv)


def _select_campaigns(project_folder: Path, wanted: list[str] | None) -> list[Campaign] | None:
    """Discovered campaigns, optionally filtered to the names in --campaign.

    Returns None on an error already reported to the log (unknown name or an
    empty selection).
    """
    campaigns = Campaign.discover(project_folder)
    if not campaigns:
        _log.error("No campaigns found under %s (each needs a campaign.toml).", project_folder)
        return None
    if wanted is None:
        return campaigns

    by_name = {c.name: c for c in campaigns}
    unknown = [name for name in wanted if name not in by_name]
    if unknown:
        _log.error(
            "Unknown campaign(s): %s. Available: %s",
            ", ".join(unknown),
            ", ".join(sorted(by_name)),
        )
        return None
    return [by_name[name] for name in wanted]


def _print_summary(result: AnalysisRunResult) -> None:
    for camp in result.campaigns:
        _log.info(
            "%s: %d detections across %d file(s), %d ARU(s) in %.1fs -> %s",
            camp.campaign_name,
            camp.detection_count,
            camp.wav_count,
            camp.aru_count,
            camp.elapsed,
            camp.detections_csv,
        )
        for warning in camp.warnings:
            _log.warning("%s: %s", camp.campaign_name, warning)
    _log.info("Done: %d campaign(s) in %.1fs.", len(result.campaigns), result.elapsed)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )

    project_folder = args.project.expanduser()
    if not paths.project_toml(project_folder).is_file():
        _log.error(
            "%s is not a project folder (no %s).",
            project_folder,
            paths.PROJECT_FILENAME,
        )
        return 1
    project = Project.load(project_folder)

    # Same runner class the GUI uses, which is what keeps output identical.
    runner = BirdnetRunner()
    model_key = runner.model_key

    campaigns = _select_campaigns(project_folder, args.campaign)
    if campaigns is None:
        return 1

    if args.force:
        selected = campaigns
    else:
        selected = []
        for camp in campaigns:
            if detection_schema.campaign_csv_for_model(camp.folder, model_key).is_file():
                _log.info(
                    "Skipping %s (already has %s). Use --force to re-run.",
                    camp.name,
                    detection_schema.detections_csv_name(model_key),
                )
                continue
            selected.append(camp)
        if not selected:
            _log.info("Nothing to do: every campaign already has a %s CSV.", model_key)
            return 0

    _log.info("Running %s on %d campaign(s) in %s.", model_key, len(selected), project_folder)
    progress = _ConsoleProgress()

    def _on_sigint(signum, frame) -> None:  # noqa: ARG001
        _log.warning("Cancel requested, stopping after the current file...")
        progress.request_cancel()

    signal.signal(signal.SIGINT, _on_sigint)
    try:
        result = runner.run(
            campaigns=selected,
            settings=project.analysis_settings,
            preferred_lang=project.preferred_species_lang,
            progress=progress,
        )
    except CancelledError:
        _log.warning("Run cancelled.")
        return 130
    except Exception:
        _log.exception("Analysis failed.")
        return 1

    _print_summary(result)
    return 0


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    sys.exit(main())
