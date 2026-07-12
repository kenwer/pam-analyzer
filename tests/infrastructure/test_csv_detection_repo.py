import csv
from pathlib import Path

from pam_analyzer.domain import VerifiedState
from pam_analyzer.infrastructure import CsvDetectionRepository
from pam_analyzer.infrastructure.paths import campaign_csv_for_model

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


def _seed_csv(out_base: Path, campaign: str, rows: list[list[str]]) -> None:
    path = campaign_csv_for_model(out_base, campaign, "BirdNET-2.4")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_HEADERS)
        w.writerows(rows)


def test_load_for_campaign_parses_numeric_and_annotation_columns(tmp_path: Path) -> None:
    _seed_csv(
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
    detections = repo.load_for_campaign(tmp_path, "east")
    assert len(detections) == 2
    assert detections[0].confidence == 0.85
    assert detections[0].verified == VerifiedState.TRUE
    assert detections[1].corrected_species == "Magpie"
    assert detections[1].comment == "uncertain id"


def test_load_combined_concatenates_campaign_csvs(tmp_path: Path) -> None:
    _seed_csv(tmp_path, "east", [_sample("east")])
    _seed_csv(tmp_path, "west", [_sample("west")])
    repo = CsvDetectionRepository()
    detections = repo.load_combined(tmp_path)
    assert {d.campaign for d in detections} == {"east", "west"}


def test_save_round_trip_preserves_edits(tmp_path: Path) -> None:
    _seed_csv(tmp_path, "east", [_sample("east")])
    repo = CsvDetectionRepository()
    detections = repo.load_for_campaign(tmp_path, "east")
    detections[0].verified = VerifiedState.TRUE
    detections[0].comment = "edited"
    repo.save(detections)

    repo2 = CsvDetectionRepository()
    reloaded = repo2.load_for_campaign(tmp_path, "east")
    assert reloaded[0].verified == VerifiedState.TRUE
    assert reloaded[0].comment == "edited"


def test_lat_lon_round_trip(tmp_path: Path) -> None:
    """Lat/Lon are core fields that map to named Detection attributes, not extra."""
    headers = _HEADERS + ["Lat", "Lon"]
    path = campaign_csv_for_model(tmp_path, "east", "BirdNET-2.4")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerow(_sample("east") + ["48.0", "11.0"])

    repo = CsvDetectionRepository()
    detections = repo.load_for_campaign(tmp_path, "east")
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
    path = campaign_csv_for_model(tmp_path, "east", "BirdNET-2.4")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerow(_sample("east") + ["mytag"])

    repo = CsvDetectionRepository()
    detections = repo.load_for_campaign(tmp_path, "east")
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
