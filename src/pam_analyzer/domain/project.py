import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import tomli_w

from . import paths
from .values import (
    DEFAULT_ANALYSIS_MODEL,
    DEFAULT_MIN_CONF,
    DEFAULT_SPECIES_LANG,
    MAX_OVERLAP_S,
    AnalysisSettings,
)


@dataclass
class _ProjectToml:
    """Mirrors the on-disk [project] table exactly.

    The min-confidence, overlap, and locale keys keep their historical
    birdnet_ prefix. Matching the key names older builds (and the legacy
    .pamproj format) already wrote means no on-disk migration is needed. The
    domain Project drops the prefix. The translation happens in
    Project.from_table and Project.save.
    """

    sdcard_name_pattern: str = "^(MSD-|2MM)"
    analysis_model: str = DEFAULT_ANALYSIS_MODEL
    birdnet_min_conf: float = DEFAULT_MIN_CONF
    birdnet_overlap: float = 0.0
    birdnet_locales: list[str] = field(default_factory=list)
    preferred_species_lang: str = DEFAULT_SPECIES_LANG
    snippet_padding_before: float = 0.0
    snippet_padding_after: float = 0.0


@dataclass(frozen=True, slots=True)
class Project:
    """Project settings persisted as pam-analyzer.toml inside the project folder.

    The folder is the project: it holds the settings file and one subfolder
    per campaign, so a project stores no paths and can be moved freely.
    """

    folder: Path
    sdcard_name_pattern: str = "^(MSD-|2MM)"  # AudioMoth (MSD-) and Song Meter (2MM serials)
    analysis_model: str = DEFAULT_ANALYSIS_MODEL  # which engine the BirdNET panel runs
    min_conf: float = DEFAULT_MIN_CONF
    overlap: float = 0.0
    locales: tuple[str, ...] = ()
    preferred_species_lang: str = DEFAULT_SPECIES_LANG
    snippet_padding_before: float = 0.0
    snippet_padding_after: float = 0.0

    @property
    def name(self) -> str:
        return self.folder.name

    @property
    def analysis_settings(self) -> AnalysisSettings:
        """The project's run parameters as an AnalysisSettings.

        Overlap is clamped to MAX_OVERLAP_S so a value written by an older
        build cannot exceed what the model accepts on the next run.
        """
        return AnalysisSettings(
            min_conf=self.min_conf,
            overlap=min(self.overlap, MAX_OVERLAP_S),
            locales=self.locales,
        )

    @classmethod
    def from_table(cls, folder: Path, table: dict) -> "Project":
        """Build a Project from a [project] TOML table, dropping unknown keys.

        Shared with the legacy .pamproj migration, whose settings keys are a
        superset of the current schema, so a new setting only needs to be added
        here and in _ProjectToml.
        """
        valid = {f.name for f in fields(_ProjectToml)}
        raw = _ProjectToml(**{k: v for k, v in table.items() if k in valid})
        return cls(
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

    @classmethod
    def load(cls, folder: Path) -> "Project":
        with open(paths.project_toml(folder), "rb") as f:
            data = tomllib.load(f)
        return cls.from_table(folder, data.get("project", {}))

    def save(self) -> None:
        raw = _ProjectToml(
            sdcard_name_pattern=self.sdcard_name_pattern,
            analysis_model=self.analysis_model,
            birdnet_min_conf=self.min_conf,
            birdnet_overlap=self.overlap,
            birdnet_locales=list(self.locales),
            preferred_species_lang=self.preferred_species_lang,
            snippet_padding_before=self.snippet_padding_before,
            snippet_padding_after=self.snippet_padding_after,
        )
        self.folder.mkdir(parents=True, exist_ok=True)
        with open(paths.project_toml(self.folder), "wb") as f:
            tomli_w.dump({"project": asdict(raw)}, f)

    @classmethod
    def create(cls, folder: Path) -> "Project":
        """Initialize folder as a project by writing a default pam-analyzer.toml."""
        project = cls(folder=folder)
        project.save()
        return project
