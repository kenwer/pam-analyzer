import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from pam_analyzer.core.utils import toml_escape


@dataclass
class ProjectSettings:
    # Project
    audio_recordings_path: str = str(Path.home())
    sdcard_name_pattern: str = '^MSD-'
    detections_output_path: str = ''  # defaults to "{audio_recordings_path}/{project_name}-detections" when empty

    # BirdNET analysis defaults
    birdnet_min_conf: float = 0.25
    birdnet_overlap: float = 0.0
    birdnet_locales: list[str] = field(default_factory=list)
    preferred_species_lang: str = 'en'

    # Examine panel defaults
    snippet_padding_before: float = 0.0
    snippet_padding_after: float = 0.0

    @classmethod
    def load(cls, path: Path) -> 'ProjectSettings':
        with open(path, 'rb') as f:
            data = tomllib.load(f)
        valid_keys = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.get('project', {}).items() if k in valid_keys}
        return cls(**kwargs)

    def save(self, path: Path) -> None:
        lines = ['[project]\n']
        for k, v in asdict(self).items():
            if v is None:  # omit, field will use its default on next load
                continue
            elif isinstance(v, bool):
                lines.append(f'{k} = {"true" if v else "false"}\n')
            elif isinstance(v, (int, float)):
                lines.append(f'{k} = {v}\n')
            elif isinstance(v, list):
                items = ', '.join(f'"{toml_escape(str(item))}"' for item in v)
                lines.append(f'{k} = [{items}]\n')
            else:
                lines.append(f'{k} = "{toml_escape(str(v))}"\n')
        path.write_text(''.join(lines))
