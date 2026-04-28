from pathlib import Path

from pam_analyzer.domain import Project
from pam_analyzer.infrastructure import TomlProjectRepository


def test_round_trip_preserves_all_fields(tmp_path: Path) -> None:
    repo = TomlProjectRepository()
    project = Project(
        path=tmp_path / "demo.pamproj",
        audio_recordings_path=tmp_path / "audio",
        sdcard_name_pattern="^FIELD-",
        detections_output_path=tmp_path / "out",
        birdnet_min_conf=0.42,
        birdnet_overlap=0.5,
        birdnet_locales=("de", "en"),
        preferred_species_lang="de",
        snippet_padding_before=1.5,
        snippet_padding_after=2.5,
    )
    repo.save(project)
    loaded = repo.load(project.path)
    assert loaded.audio_recordings_path == project.audio_recordings_path
    assert loaded.sdcard_name_pattern == project.sdcard_name_pattern
    assert loaded.detections_output_path == project.detections_output_path
    assert loaded.birdnet_min_conf == 0.42
    assert loaded.birdnet_overlap == 0.5
    assert loaded.birdnet_locales == ("de", "en")
    assert loaded.preferred_species_lang == "de"
    assert loaded.snippet_padding_before == 1.5
    assert loaded.snippet_padding_after == 2.5


def test_load_falls_back_to_defaults_for_missing_keys(tmp_path: Path) -> None:
    p = tmp_path / "minimal.pamproj"
    p.write_text('[project]\naudio_recordings_path = "/tmp/audio"\n')
    project = TomlProjectRepository().load(p)
    assert project.audio_recordings_path == Path("/tmp/audio")
    assert project.birdnet_min_conf == 0.25  # default
    assert project.birdnet_locales == ()
