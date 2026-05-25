"""Reads/writes .pamproj TOML files. Drop-in compatible with the original schema."""

import tomllib
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

import tomli_w

from ..domain import Project


@dataclass
class _ProjectToml:
    """Mirrors the original ProjectSettings TOML schema exactly."""

    audio_recordings_path: str = ""
    sdcard_name_pattern: str = "^MSD-"
    detections_output_path: str = ""
    analysis_model: str = "BirdNET"
    birdnet_min_conf: float = 0.25
    birdnet_overlap: float = 0.0
    birdnet_locales: list[str] = field(default_factory=list)
    preferred_species_lang: str = "en"
    snippet_padding_before: float = 0.0
    snippet_padding_after: float = 0.0


class TomlProjectRepository:
    def load(self, path: Path) -> Project:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        valid = {f.name for f in fields(_ProjectToml)}
        kwargs = {k: v for k, v in data.get("project", {}).items() if k in valid}
        raw = _ProjectToml(**kwargs)
        audio_path = Path(raw.audio_recordings_path) if raw.audio_recordings_path else Path.home()
        out_path = Path(raw.detections_output_path) if raw.detections_output_path else None
        return Project(
            path=path,
            audio_recordings_path=audio_path,
            sdcard_name_pattern=raw.sdcard_name_pattern,
            detections_output_path=out_path,
            analysis_model=raw.analysis_model,
            birdnet_min_conf=raw.birdnet_min_conf,
            birdnet_overlap=raw.birdnet_overlap,
            birdnet_locales=tuple(raw.birdnet_locales),
            preferred_species_lang=raw.preferred_species_lang,
            snippet_padding_before=raw.snippet_padding_before,
            snippet_padding_after=raw.snippet_padding_after,
        )

    def save(self, project: Project) -> None:
        raw = _ProjectToml(
            audio_recordings_path=str(project.audio_recordings_path),
            sdcard_name_pattern=project.sdcard_name_pattern,
            detections_output_path=(str(project.detections_output_path) if project.detections_output_path else ""),
            analysis_model=project.analysis_model,
            birdnet_min_conf=project.birdnet_min_conf,
            birdnet_overlap=project.birdnet_overlap,
            birdnet_locales=list(project.birdnet_locales),
            preferred_species_lang=project.preferred_species_lang,
            snippet_padding_before=project.snippet_padding_before,
            snippet_padding_after=project.snippet_padding_after,
        )
        project.path.parent.mkdir(parents=True, exist_ok=True)
        with open(project.path, "wb") as f:
            tomli_w.dump({"project": asdict(raw)}, f)

    def create(self, path: Path) -> Project:
        """Create a new project file at path with default audio root = path.parent."""
        project = Project(path=path, audio_recordings_path=path.parent)
        self.save(project)
        return project

    def save_as(self, project: Project, new_path: Path) -> Project:
        """Persist project under new_path and return the rebound copy."""
        rebound = replace(project, path=new_path)
        self.save(rebound)
        return rebound
