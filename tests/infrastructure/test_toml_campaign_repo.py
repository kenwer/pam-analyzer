"""Round-trip tests for TomlCampaignRepository's new methods."""

from pathlib import Path

import pytest

from pam_analyzer.domain import Campaign, FilterMode, LatLon
from pam_analyzer.infrastructure.toml_campaign_repo import TomlCampaignRepository


@pytest.fixture
def repo():
    return TomlCampaignRepository()


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


def test_create_writes_folder_and_toml(repo, audio_root):
    c = _new_campaign(audio_root, "site-alpha")
    repo.create(c)
    assert c.folder.exists()
    toml_path = c.folder / "campaign.toml"
    assert toml_path.exists()
    assert "species_filter_mode" in toml_path.read_text()


def test_create_raises_on_duplicate(repo, audio_root):
    c = _new_campaign(audio_root, "dup")
    repo.create(c)
    with pytest.raises(FileExistsError):
        repo.create(_new_campaign(audio_root, "dup"))


def test_rename_moves_folder(repo, audio_root):
    old = _new_campaign(audio_root, "old-name")
    repo.create(old)
    renamed = repo.rename(old, "new-name")
    assert renamed.name == "new-name"
    assert renamed.folder == audio_root / "new-name"
    assert renamed.folder.exists()
    assert not old.folder.exists()


def test_rename_preserves_mode_and_location(repo, audio_root):
    c = _new_campaign(audio_root, "with-loc", LatLon(48.1, 11.5))
    repo.create(c)
    renamed = repo.rename(c, "renamed-loc")
    assert renamed.location == LatLon(48.1, 11.5)
    assert renamed.species_filter_mode == FilterMode.LOCATION


def test_rename_keeps_detection_csvs_valid(repo, audio_root):
    """CSV names and File paths carry no campaign name, so a folder rename
    leaves the campaign's detections fully usable."""
    from pam_analyzer.infrastructure import CsvDetectionRepository, paths

    c = _new_campaign(audio_root, "before")
    repo.create(c)
    csv_path = paths.campaign_csv_for_model(c.folder, "BirdNET-2.4")
    csv_path.write_text(
        "Campaign,Species,Confidence,File\nbefore,Robin,0.9,MSD-1/week_08/r.flac\n",
        encoding="utf-8",
    )

    renamed = repo.rename(c, "after")

    assert paths.campaign_csvs(renamed.folder) == [renamed.folder / "detections-BirdNET-2.4.csv"]
    detections = CsvDetectionRepository().load_for_campaign(renamed.folder)
    assert detections[0].file == "after/MSD-1/week_08/r.flac"
    assert (audio_root / detections[0].file).parent == renamed.folder / "MSD-1" / "week_08"


def test_delete_removes_entire_folder(repo, audio_root):
    c = _new_campaign(audio_root, "to-delete")
    repo.create(c)
    (c.folder / "recording.wav").write_bytes(b"RIFF")
    repo.delete(c)
    assert not c.folder.exists()


def test_count_audio_files_counts_by_extension(repo, audio_root):
    c = _new_campaign(audio_root, "with-audio")
    repo.create(c)
    (c.folder / "a.wav").write_bytes(b"")
    (c.folder / "b.WAV").write_bytes(b"")  # uppercase should match
    (c.folder / "c.flac").write_bytes(b"")
    (c.folder / "d.mp3").write_bytes(b"")
    (c.folder / "notes.txt").write_bytes(b"")  # not audio
    assert repo.count_audio_files(c) == 4


def test_count_audio_files_recurses(repo, audio_root):
    c = _new_campaign(audio_root, "with-sub")
    repo.create(c)
    sub = c.folder / "subdir"
    sub.mkdir()
    (sub / "deep.wav").write_bytes(b"")
    assert repo.count_audio_files(c) == 1


def test_count_audio_files_empty(repo, audio_root):
    c = _new_campaign(audio_root, "empty-audio")
    repo.create(c)
    assert repo.count_audio_files(c) == 0


def test_species_list_read_write_roundtrip(repo, audio_root):
    c = _new_campaign(audio_root, "species-test")
    repo.create(c)
    assert repo.read_species_list(c) == ""
    repo.write_species_list(c, "Robin\nBlackcap\n")
    result = repo.read_species_list(c)
    assert "Robin" in result
    assert "Blackcap" in result


def test_load_after_save(repo, audio_root):
    c = _new_campaign(audio_root, "roundtrip", LatLon(51.5, -0.1))
    repo.create(c)
    loaded = repo.load(c.name, c.folder)
    assert loaded.location is not None
    assert abs(loaded.location.latitude - 51.5) < 1e-6
    assert abs(loaded.location.longitude - (-0.1)) < 1e-6
