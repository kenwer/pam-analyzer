from pathlib import Path

from pam_analyzer.domain import Project
from pam_analyzer.infrastructure import TomlProjectRepository, paths


def test_round_trip_preserves_all_fields(tmp_path: Path) -> None:
    repo = TomlProjectRepository()
    project = Project(
        folder=tmp_path / "demo",
        sdcard_name_pattern="^FIELD-",
        birdnet_min_conf=0.42,
        birdnet_overlap=0.5,
        birdnet_locales=("de", "en"),
        preferred_species_lang="de",
        snippet_padding_before=1.5,
        snippet_padding_after=2.5,
    )
    repo.save(project)
    loaded = repo.load(project.folder)
    assert loaded == project
    assert loaded.name == "demo"


def test_load_falls_back_to_defaults_for_missing_keys(tmp_path: Path) -> None:
    paths.project_toml(tmp_path).write_text('[project]\nsdcard_name_pattern = "^X-"\n')
    project = TomlProjectRepository().load(tmp_path)
    assert project.sdcard_name_pattern == "^X-"
    assert project.birdnet_min_conf == 0.25  # default
    assert project.birdnet_locales == ()


def test_load_ignores_legacy_path_keys(tmp_path: Path) -> None:
    paths.project_toml(tmp_path).write_text(
        "[project]\n"
        'audio_recordings_path = "/somewhere/else"\n'
        'detections_output_path = "/elsewhere"\n'
        "birdnet_min_conf = 0.4\n"
    )
    project = TomlProjectRepository().load(tmp_path)
    assert project.folder == tmp_path
    assert project.birdnet_min_conf == 0.4


def test_create_writes_default_project_file(tmp_path: Path) -> None:
    folder = tmp_path / "new-project"
    project = TomlProjectRepository().create(folder)
    assert project.folder == folder
    assert paths.project_toml(folder).exists()
