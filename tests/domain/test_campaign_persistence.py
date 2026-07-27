"""Round-trip tests for Campaign's self-persistence methods."""

from pathlib import Path

import pytest

from pam_analyzer.domain import Campaign, FilterMode, LatLon


@pytest.fixture
def audio_root(tmp_path: Path) -> Path:
    root = tmp_path / "audio"
    root.mkdir()
    return root


def _new_campaign(audio_root: Path, name: str, location: LatLon | None = None) -> Campaign:
    return Campaign(
        name=name,
        folder=audio_root / name,
        species_filter_mode=FilterMode.LOCATION,
        location=location or LatLon(0.0, 0.0),
    )


def test_create_writes_folder_and_toml(audio_root):
    c = _new_campaign(audio_root, "site-alpha")
    c.create()
    assert c.folder.exists()
    toml_path = c.folder / "campaign.toml"
    assert toml_path.exists()
    assert "species_filter_mode" in toml_path.read_text()


def test_create_raises_on_duplicate(audio_root):
    c = _new_campaign(audio_root, "dup")
    c.create()
    with pytest.raises(FileExistsError):
        _new_campaign(audio_root, "dup").create()


def test_rename_moves_folder(audio_root):
    old = _new_campaign(audio_root, "old-name")
    old.create()
    renamed = old.rename("new-name")
    assert renamed.name == "new-name"
    assert renamed.folder == audio_root / "new-name"
    assert renamed.folder.exists()
    assert not old.folder.exists()


def test_rename_preserves_mode_and_location(audio_root):
    c = _new_campaign(audio_root, "with-loc", LatLon(48.1, 11.5))
    c.create()
    renamed = c.rename("renamed-loc")
    assert renamed.location == LatLon(48.1, 11.5)
    assert renamed.species_filter_mode == FilterMode.LOCATION


def test_rename_keeps_detection_csvs_valid(audio_root):
    """CSV names and File paths carry no campaign name, so a folder rename
    leaves the campaign's detections fully usable."""
    from pam_analyzer.domain import DetectionSet
    from pam_analyzer.domain import detection_schema as schema

    c = _new_campaign(audio_root, "before")
    c.create()
    csv_path = schema.campaign_csv_for_model(c.folder, "BirdNET-2.4")
    csv_path.write_text(
        "Campaign,Species,Confidence,File\nbefore,Robin,0.9,MSD-1/week_08/r.flac\n",
        encoding="utf-8",
    )

    renamed = c.rename("after")

    assert schema.campaign_csvs(renamed.folder) == [renamed.folder / "detections-BirdNET-2.4.csv"]
    detections = DetectionSet.load_for_campaign(renamed.folder).detections
    assert detections[0].file == "after/MSD-1/week_08/r.flac"
    assert (audio_root / detections[0].file).parent == renamed.folder / "MSD-1" / "week_08"


def test_delete_removes_entire_folder(audio_root):
    c = _new_campaign(audio_root, "to-delete")
    c.create()
    (c.folder / "recording.wav").write_bytes(b"RIFF")
    c.delete()
    assert not c.folder.exists()


def test_count_audio_files_counts_by_extension(audio_root):
    c = _new_campaign(audio_root, "with-audio")
    c.create()
    (c.folder / "a.wav").write_bytes(b"")
    (c.folder / "b.WAV").write_bytes(b"")  # uppercase should match
    (c.folder / "c.flac").write_bytes(b"")
    (c.folder / "d.mp3").write_bytes(b"")
    (c.folder / "notes.txt").write_bytes(b"")  # not audio
    assert c.count_audio_files() == 4


def test_count_audio_files_recurses(audio_root):
    c = _new_campaign(audio_root, "with-sub")
    c.create()
    sub = c.folder / "subdir"
    sub.mkdir()
    (sub / "deep.wav").write_bytes(b"")
    assert c.count_audio_files() == 1


def test_count_audio_files_empty(audio_root):
    c = _new_campaign(audio_root, "empty-audio")
    c.create()
    assert c.count_audio_files() == 0


def test_species_list_read_write_roundtrip(audio_root):
    c = _new_campaign(audio_root, "species-test")
    c.create()
    assert c.read_species_list() == ""
    c.write_species_list("Robin\nBlackcap\n")
    result = c.read_species_list()
    assert "Robin" in result
    assert "Blackcap" in result


def test_load_after_save(audio_root):
    c = _new_campaign(audio_root, "roundtrip", LatLon(51.5, -0.1))
    c.create()
    loaded = Campaign.load(c.name, c.folder)
    assert loaded.location is not None
    assert abs(loaded.location.latitude - 51.5) < 1e-6
    assert abs(loaded.location.longitude - (-0.1)) < 1e-6
