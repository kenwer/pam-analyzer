"""One-time migration of legacy standalone .pamproj projects.

The legacy format was a freely named .pamproj file holding two absolute
paths (audio_recordings_path, detections_output_path) plus settings, with
detection CSVs in a parallel tree:

    <output_base>/<campaign>/<campaign>-detections-<model>.csv

The current format is a self-contained project folder (the old audio root)
with a hardcoded pam-analyzer.toml and per-campaign CSVs:

    <project>/<campaign>/detections-<model>.csv

migrate() moves each CSV into its campaign folder, rewriting the File
column from project-relative to campaign-relative, then writes
pam-analyzer.toml and renames the legacy file to .bak. Each CSV move is
individually atomic and existing destinations are never overwritten, so a
crashed or repeated migration is safe to rerun.
"""

import csv
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ..domain import Project
from . import paths
from .toml_project_repo import TomlProjectRepository, project_from_table


@dataclass(frozen=True)
class LegacyProject:
    pamproj_path: Path
    audio_root: Path
    output_base: Path
    project: Project  # new-style entity: folder=audio_root, settings carried over


@dataclass(frozen=True)
class MigrationReport:
    project_folder: Path
    moved_csvs: int
    warnings: tuple[str, ...]


class AudioRootNotFound(ValueError):
    """The legacy file's audio_recordings_path doesn't exist on this machine.

    Common after moving a project between machines or remounting a network
    share under a different path. Callers can catch this specifically to
    offer the user a folder picker and retry via load_legacy's audio_root
    override, instead of just failing the migration outright.
    """

    def __init__(self, recorded_path: str) -> None:
        self.recorded_path = recorded_path
        super().__init__(f"Audio recording folder does not exist: {recorded_path}")


def find_legacy_pamproj(folder: Path) -> Path | None:
    """The single legacy .pamproj file directly in folder, if any.

    Returns None when there is no candidate or more than one (ambiguous).
    """
    candidates = [p for p in folder.glob("*.pamproj") if p.is_file()]
    return candidates[0] if len(candidates) == 1 else None


def load_legacy(pamproj_path: Path, *, audio_root: Path | None = None) -> LegacyProject:
    """Parse a legacy .pamproj file.

    audio_root overrides the file's own audio_recordings_path, for when the
    caller already knows the recorded path isn't valid on this machine (e.g.
    after the user redirected it via a folder picker).

    Raises ValueError when the file has no usable audio_recordings_path.
    Raises AudioRootNotFound when neither the override nor the recorded path
    is an existing directory, since the audio root is what becomes the
    project folder.
    """
    with open(pamproj_path, "rb") as f:
        data = tomllib.load(f)
    table = data.get("project", {})

    if audio_root is None:
        audio_raw = str(table.get("audio_recordings_path", "") or "")
        if not audio_raw:
            raise ValueError(f"{pamproj_path.name} has no audio_recordings_path")
        audio_root = Path(audio_raw)
        if not audio_root.is_dir():
            raise AudioRootNotFound(audio_raw)
    elif not audio_root.is_dir():
        raise AudioRootNotFound(str(audio_root))

    out_raw = str(table.get("detections_output_path", "") or "")
    output_base = Path(out_raw) if out_raw else audio_root / f"{pamproj_path.stem}-detections"

    # The settings subset of the legacy schema is identical to the current
    # one; project_from_table drops the legacy path keys and fills defaults.
    project = project_from_table(audio_root, table)
    return LegacyProject(
        pamproj_path=pamproj_path,
        audio_root=audio_root,
        output_base=output_base,
        project=project,
    )


def migrate(legacy: LegacyProject) -> MigrationReport:
    """Convert a legacy project in place and return what happened.

    Order matters for crash safety: CSVs move first, pam-analyzer.toml is
    written next, and the legacy file is renamed to .bak last, so an
    interrupted migration leaves the legacy project openable and this
    function rerunnable.
    """
    warnings: list[str] = []
    moved = 0

    for campaign_folder in paths.campaign_folders(legacy.audio_root):
        moved += _migrate_campaign(campaign_folder, legacy.output_base, warnings)

    _cleanup_output_tree(legacy.output_base, legacy.audio_root, warnings)

    TomlProjectRepository().save(legacy.project)

    if legacy.pamproj_path != paths.project_toml(legacy.audio_root):
        backup = legacy.pamproj_path.with_name(legacy.pamproj_path.name + ".bak")
        try:
            os.replace(legacy.pamproj_path, backup)
        except OSError as exc:
            warnings.append(f"Could not rename {legacy.pamproj_path.name} to .bak: {exc}")

    return MigrationReport(
        project_folder=legacy.audio_root,
        moved_csvs=moved,
        warnings=tuple(warnings),
    )


def _migrate_campaign(campaign_folder: Path, output_base: Path, warnings: list[str]) -> int:
    """Move one campaign's legacy outputs into its folder. Returns CSVs moved."""
    name = campaign_folder.name
    legacy_dir = output_base / name
    if not legacy_dir.is_dir():
        return 0

    moved = 0
    csv_prefix = f"{name}-detections-"
    species_list_re = re.compile(re.escape(name) + r"-species-list(-week-\d{2})?\.txt")
    for src in sorted(legacy_dir.iterdir()):
        if not src.is_file():
            continue
        if src.name.startswith(csv_prefix) and src.name.endswith(".csv"):
            model_key = src.name[len(csv_prefix):-len(".csv")]
            dest = campaign_folder / f"detections-{model_key}.csv"
            if dest.exists():
                warnings.append(f"Skipped {src.name}: {dest.name} already exists in {name}")
                continue
            _move_csv_rewriting_file_column(src, dest, name)
            moved += 1
        elif species_list_re.fullmatch(src.name):
            new_name = "applied" + src.name[len(name):]
            dest = campaign_folder / new_name
            if dest.exists():
                warnings.append(f"Skipped {src.name}: {dest.name} already exists in {name}")
                continue
            os.replace(src, dest)
    return moved


def _move_csv_rewriting_file_column(src: Path, dest: Path, campaign_name: str) -> None:
    """Copy src to dest with File cells made campaign-relative, then delete src.

    A plain DictReader/DictWriter pass with identical fieldnames is lossless:
    unknown columns and column order are preserved exactly. The .part temp
    plus os.replace keeps the destination atomic.
    """
    prefix = f"{campaign_name}/"
    tmp = dest.with_name(dest.name + ".part")
    try:
        with open(src, encoding="utf-8", newline="") as inf:
            reader = csv.DictReader(inf)
            fieldnames = list(reader.fieldnames or [])
            with open(tmp, "w", encoding="utf-8", newline="") as outf:
                writer = csv.DictWriter(outf, fieldnames=fieldnames)
                writer.writeheader()
                for row in reader:
                    file_cell = row.get("File", "")
                    if file_cell.startswith(prefix):
                        row["File"] = file_cell[len(prefix):]
                    writer.writerow(row)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    src.unlink()


def _cleanup_output_tree(output_base: Path, audio_root: Path, warnings: list[str]) -> None:
    """Best-effort removal of emptied legacy output dirs.

    Never touches audio_root itself or any campaign folder, which covers the
    degenerate configuration where output_base pointed into (or at) the
    audio root. Non-empty dirs are left in place with a warning so nothing
    the migration did not understand gets deleted.
    """
    if not output_base.is_dir():
        return
    audio_root_resolved = audio_root.resolve()
    for sub in sorted(output_base.iterdir()):
        if not sub.is_dir():
            continue
        if sub.resolve() == audio_root_resolved or paths.campaign_toml(sub).exists():
            continue
        try:
            sub.rmdir()
        except OSError:
            warnings.append(f"Left non-empty legacy output folder in place: {sub}")
    if output_base.resolve() != audio_root_resolved:
        try:
            output_base.rmdir()
        except OSError:
            warnings.append(f"Left non-empty legacy output folder in place: {output_base}")
