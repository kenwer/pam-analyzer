"""Reads/writes the pam-analyzer.toml TOML file inside a project folder.

The file stores settings only, never paths: the folder it lives in IS the
project, so projects stay relocatable. Unknown keys (including the path
fields of the legacy standalone .pamproj format) are dropped on load and
missing keys fall back to dataclass defaults.
"""

import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import tomli_w

from ..domain import Project
from . import paths


@dataclass
class _ProjectToml:
    """Mirrors the on-disk [project] table exactly.

    The min-confidence, overlap, and locale keys keep their historical
    birdnet_ prefix even though every model now uses them. Matching the key
    names older builds (and the legacy .pamproj format) already wrote means
    no on-disk migration is needed. The domain Project drops the prefix; the
    translation happens in project_from_table and save.
    """

    sdcard_name_pattern: str = "^(MSD-|2MM)"
    analysis_model: str = "BirdNET-2.4"
    birdnet_min_conf: float = 0.25
    birdnet_overlap: float = 0.0
    birdnet_locales: list[str] = field(default_factory=list)
    preferred_species_lang: str = "en"
    snippet_padding_before: float = 0.0
    snippet_padding_after: float = 0.0


def project_from_table(folder: Path, table: dict) -> Project:
    """Build a Project from a [project] TOML table, dropping unknown keys.

    Shared with the legacy .pamproj migration, whose settings keys are a
    superset of the current schema, so a new setting only needs to be added
    here and in _ProjectToml.
    """
    valid = {f.name for f in fields(_ProjectToml)}
    raw = _ProjectToml(**{k: v for k, v in table.items() if k in valid})
    return Project(
        folder=folder,
        sdcard_name_pattern=raw.sdcard_name_pattern,
        analysis_model=raw.analysis_model,
        min_conf=raw.birdnet_min_conf,
        overlap=raw.birdnet_overlap,
        locales=tuple(raw.birdnet_locales),
        preferred_species_lang=raw.preferred_species_lang,
        snippet_padding_before=raw.snippet_padding_before,
        snippet_padding_after=raw.snippet_padding_after,
    )


class TomlProjectRepository:
    def load(self, folder: Path) -> Project:
        with open(paths.project_toml(folder), "rb") as f:
            data = tomllib.load(f)
        return project_from_table(folder, data.get("project", {}))

    def save(self, project: Project) -> None:
        raw = _ProjectToml(
            sdcard_name_pattern=project.sdcard_name_pattern,
            analysis_model=project.analysis_model,
            birdnet_min_conf=project.min_conf,
            birdnet_overlap=project.overlap,
            birdnet_locales=list(project.locales),
            preferred_species_lang=project.preferred_species_lang,
            snippet_padding_before=project.snippet_padding_before,
            snippet_padding_after=project.snippet_padding_after,
        )
        project.folder.mkdir(parents=True, exist_ok=True)
        with open(paths.project_toml(project.folder), "wb") as f:
            tomli_w.dump({"project": asdict(raw)}, f)

    def create(self, folder: Path) -> Project:
        """Initialize folder as a project by writing a default pam-analyzer.toml."""
        project = Project(folder=folder)
        self.save(project)
        return project
