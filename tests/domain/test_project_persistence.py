import tomllib
from pathlib import Path

from pam_analyzer.domain import (
    DEFAULT_ANALYSIS_MODEL,
    DEFAULT_MIN_CONF,
    Project,
    paths,
)


def test_round_trip_preserves_all_fields(tmp_path: Path) -> None:
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
    project.save()
    loaded = Project.load(project.folder)
    assert loaded == project
    assert loaded.name == "demo"


def test_load_falls_back_to_defaults_for_missing_keys(tmp_path: Path) -> None:
    paths.project_toml(tmp_path).write_text('[project]\nsdcard_name_pattern = "^X-"\n')
    project = Project.load(tmp_path)
    assert project.sdcard_name_pattern == "^X-"
    assert project.min_conf == DEFAULT_MIN_CONF
    assert project.analysis_model == DEFAULT_ANALYSIS_MODEL
    assert project.locales == ()


def test_load_ignores_legacy_path_keys(tmp_path: Path) -> None:
    paths.project_toml(tmp_path).write_text(
        "[project]\n"
        'audio_recordings_path = "/somewhere/else"\n'
        'detections_output_path = "/elsewhere"\n'
        "birdnet_min_conf = 0.4\n"
    )
    project = Project.load(tmp_path)
    assert project.folder == tmp_path
    assert project.min_conf == 0.4


def test_save_writes_birdnet_prefixed_keys(tmp_path: Path) -> None:
    """Lock the on-disk format: the file uses birdnet_ keys, not the plain
    domain field names, so existing project files stay readable."""
    project = Project(folder=tmp_path / "p", min_conf=0.3, overlap=0.7, locales=("de",))
    project.save()
    with open(paths.project_toml(project.folder), "rb") as f:
        table = tomllib.load(f)["project"]
    assert table["birdnet_min_conf"] == 0.3
    assert table["birdnet_overlap"] == 0.7
    assert table["birdnet_locales"] == ["de"]
    assert "min_conf" not in table


def test_a_project_file_naming_the_old_axis_setting_still_loads(tmp_path):
    """from_table filters to known keys, so a stale analysis_taxonomy is
    ignored rather than raising. No migration code is needed.
    """
    project = Project.from_table(
        tmp_path,
        {"analysis_taxonomy": "BirdNET-2.4", "analysis_model": "BirdNET-2.4"},
    )

    assert project.analysis_model == "BirdNET-2.4"
    assert not hasattr(project, "analysis_taxonomy")


def test_a_project_saved_on_the_v3_preview_opens_on_the_final_v3(tmp_path):
    project = Project.from_table(tmp_path, {"analysis_model": "BirdNET-3.0-preview3.1"})

    assert project.analysis_model == "BirdNET-3.0"
