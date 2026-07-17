import csv
from pathlib import Path

from pam_analyzer.domain import VerifiedState
from pam_analyzer.infrastructure import CsvDetectionRepository
from pam_analyzer.infrastructure.paths import campaign_csv_for_model, campaign_toml

_HEADERS = [
    "Campaign",
    "ARU",
    "Week",
    "Species",
    "Scientific_Name",
    "Confidence",
    "Start_Time",
    "End_Time",
    "Rank",
    "File",
    "Recording_Time",
    "Verified",
    "Corrected_Species",
    "Comment",
]


def _campaign_dir(project_folder: Path, campaign: str) -> Path:
    folder = project_folder / campaign
    folder.mkdir(parents=True, exist_ok=True)
    campaign_toml(folder).write_text("", encoding="utf-8")
    return folder


def _seed_csv(project_folder: Path, campaign: str, rows: list[list[str]]) -> Path:
    folder = _campaign_dir(project_folder, campaign)
    path = campaign_csv_for_model(folder, "BirdNET-2.4")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_HEADERS)
        w.writerows(rows)
    return folder


def test_load_for_campaign_parses_numeric_and_annotation_columns(tmp_path: Path) -> None:
    folder = _seed_csv(
        tmp_path,
        "east",
        [
            [
                "east",
                "MSD-1",
                "24",
                "Robin",
                "Erithacus rubecula",
                "0.85",
                "0.0",
                "3.0",
                "1",
                "f.wav",
                "2026-04-25T08:00:00",
                "true",
                "",
                "",
            ],
            [
                "east",
                "MSD-1",
                "24",
                "Crow",
                "Corvus corone",
                "0.5",
                "3.0",
                "6.0",
                "2",
                "f.wav",
                "2026-04-25T08:00:00",
                "",
                "Magpie",
                "uncertain id",
            ],
        ],
    )
    repo = CsvDetectionRepository()
    detections = repo.load_for_campaign(folder)
    assert len(detections) == 2
    assert detections[0].confidence == 0.85
    assert detections[0].verified == VerifiedState.TRUE
    assert detections[1].corrected_species == "Magpie"
    assert detections[1].comment == "uncertain id"


def test_load_prefixes_file_with_campaign_folder_name(tmp_path: Path) -> None:
    """On disk File is campaign-relative; in memory it is project-relative."""
    folder = _seed_csv(tmp_path, "east", [_sample("east")])
    detections = CsvDetectionRepository().load_for_campaign(folder)
    assert detections[0].file == "east/f.wav"


def test_load_combined_concatenates_campaign_csvs(tmp_path: Path) -> None:
    _seed_csv(tmp_path, "east", [_sample("east")])
    _seed_csv(tmp_path, "west", [_sample("west")])
    repo = CsvDetectionRepository()
    detections = repo.load_combined(tmp_path)
    assert {d.campaign for d in detections} == {"east", "west"}


def test_load_combined_skips_non_campaign_dirs(tmp_path: Path) -> None:
    _seed_csv(tmp_path, "east", [_sample("east")])
    stray = tmp_path / "not-a-campaign"
    stray.mkdir()
    with open(stray / "detections-BirdNET-2.4.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_HEADERS)
        w.writerow(_sample("stray"))
    detections = CsvDetectionRepository().load_combined(tmp_path)
    assert {d.campaign for d in detections} == {"east"}


def test_save_round_trip_preserves_edits(tmp_path: Path) -> None:
    folder = _seed_csv(tmp_path, "east", [_sample("east")])
    repo = CsvDetectionRepository()
    detections = repo.load_for_campaign(folder)
    detections[0].verified = VerifiedState.TRUE
    detections[0].comment = "edited"
    repo.save(detections)

    repo2 = CsvDetectionRepository()
    reloaded = repo2.load_for_campaign(folder)
    assert reloaded[0].verified == VerifiedState.TRUE
    assert reloaded[0].comment == "edited"


def test_save_keeps_file_campaign_relative_on_disk(tmp_path: Path) -> None:
    """Saving an edit must not leak the in-memory campaign prefix to disk."""
    folder = _seed_csv(tmp_path, "east", [_sample("east")])
    repo = CsvDetectionRepository()
    detections = repo.load_for_campaign(folder)
    detections[0].comment = "edited"
    repo.save(detections)

    with open(campaign_csv_for_model(folder, "BirdNET-2.4"), encoding="utf-8") as f:
        row = next(csv.DictReader(f))
    assert row["File"] == "f.wav"
    # The in-memory detection stays project-relative even after the save.
    assert detections[0].file == "east/f.wav"


def test_save_failure_leaves_original_file_intact(tmp_path: Path, monkeypatch) -> None:
    """A crash mid-write must not truncate the CSV holding user annotations.

    The write goes to a '.part' sibling that is swapped in atomically, so a
    serialization failure leaves the original bytes untouched and no temp
    file behind.
    """
    folder = _seed_csv(tmp_path, "east", [_sample("east")])
    csv_path = campaign_csv_for_model(folder, "BirdNET-2.4")
    original_bytes = csv_path.read_bytes()

    repo = CsvDetectionRepository()
    detections = repo.load_for_campaign(folder)

    def _boom(_d):
        raise RuntimeError("simulated crash mid-serialization")

    monkeypatch.setattr(
        "pam_analyzer.infrastructure.csv_detection_repo.schema.detection_to_row", _boom
    )
    try:
        repo.save(detections)
    except RuntimeError:
        pass

    assert csv_path.read_bytes() == original_bytes
    assert not list(csv_path.parent.glob("*.part"))


def test_save_leaves_no_temp_file(tmp_path: Path) -> None:
    folder = _seed_csv(tmp_path, "east", [_sample("east")])
    repo = CsvDetectionRepository()
    repo.save(repo.load_for_campaign(folder))
    csv_path = campaign_csv_for_model(folder, "BirdNET-2.4")
    assert not list(csv_path.parent.glob("*.part"))


def test_lat_lon_round_trip(tmp_path: Path) -> None:
    """Lat/Lon are core fields that map to named Detection attributes, not extra."""
    headers = _HEADERS + ["Lat", "Lon"]
    folder = _campaign_dir(tmp_path, "east")
    path = campaign_csv_for_model(folder, "BirdNET-2.4")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerow(_sample("east") + ["48.0", "11.0"])

    repo = CsvDetectionRepository()
    detections = repo.load_for_campaign(folder)
    assert detections[0].lat == 48.0
    assert detections[0].lon == 11.0
    assert "Lat" not in detections[0].extra
    assert "Lon" not in detections[0].extra

    repo.save(detections)
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        row = next(reader)
    assert row["Lat"] == "48"
    assert row["Lon"] == "11"


def test_truly_unknown_columns_go_to_extra(tmp_path: Path) -> None:
    """Columns outside detection_schema.CORE_FIELDS still land in Detection.extra."""
    headers = _HEADERS + ["CustomTag"]
    folder = _campaign_dir(tmp_path, "east")
    path = campaign_csv_for_model(folder, "BirdNET-2.4")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerow(_sample("east") + ["mytag"])

    repo = CsvDetectionRepository()
    detections = repo.load_for_campaign(folder)
    assert detections[0].extra == {"CustomTag": "mytag"}


def _sample(campaign: str) -> list[str]:
    return [
        campaign,
        "MSD-1",
        "24",
        "Robin",
        "Erithacus rubecula",
        "0.85",
        "0.0",
        "3.0",
        "1",
        "f.wav",
        "2026-04-25T08:00:00",
        "",
        "",
        "",
    ]
