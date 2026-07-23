import tomllib
from pathlib import Path

from pam_analyzer.domain import Project
from pam_analyzer.infrastructure import TomlProjectRepository, paths


def test_round_trip_preserves_all_fields(tmp_path: Path) -> None:
    repo = TomlProjectRepository()
    project = Project(
        folder=tmp_path / "demo",
        sdcard_name_pattern="^FIELD-",
        min_conf=0.42,
        overlap=0.5,
        locales=("de", "en"),
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
    assert project.min_conf == 0.25  # default
    assert project.locales == ()


def test_load_ignores_legacy_path_keys(tmp_path: Path) -> None:
    paths.project_toml(tmp_path).write_text(
        "[project]\n"
        'audio_recordings_path = "/somewhere/else"\n'
        'detections_output_path = "/elsewhere"\n'
        "birdnet_min_conf = 0.4\n"
    )
    project = TomlProjectRepository().load(tmp_path)
    assert project.folder == tmp_path
    assert project.min_conf == 0.4


def test_reads_birdnet_prefixed_keys(tmp_path: Path) -> None:
    """On disk the knobs keep their birdnet_ prefix; the domain drops it."""
    paths.project_toml(tmp_path).write_text(
        "[project]\n"
        "birdnet_min_conf = 0.4\n"
        "birdnet_overlap = 1.2\n"
        'birdnet_locales = ["de", "fr"]\n'
    )
    project = TomlProjectRepository().load(tmp_path)
    assert project.min_conf == 0.4
    assert project.overlap == 1.2
    assert project.locales == ("de", "fr")


def test_save_writes_birdnet_prefixed_keys(tmp_path: Path) -> None:
    """Lock the on-disk format: the file uses birdnet_ keys, not the plain
    domain field names, so existing project files stay readable."""
    project = Project(folder=tmp_path / "p", min_conf=0.3, overlap=0.7, locales=("de",))
    TomlProjectRepository().save(project)
    with open(paths.project_toml(project.folder), "rb") as f:
        table = tomllib.load(f)["project"]
    assert table["birdnet_min_conf"] == 0.3
    assert table["birdnet_overlap"] == 0.7
    assert table["birdnet_locales"] == ["de"]
    assert "min_conf" not in table


def test_create_writes_default_project_file(tmp_path: Path) -> None:
    folder = tmp_path / "new-project"
    project = TomlProjectRepository().create(folder)
    assert project.folder == folder
    assert paths.project_toml(folder).exists()
