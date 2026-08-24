import csv
from pathlib import Path

import pytest

from pam_analyzer.domain import DetectionSet, VerifiedState
from pam_analyzer.domain.detection_schema import campaign_csv_for_model
from pam_analyzer.domain.paths import campaign_toml
from tests.conftest import DEFAULT_MODEL_KEY, RETIRED_MODEL_KEYS

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


@pytest.fixture(params=[DEFAULT_MODEL_KEY, RETIRED_MODEL_KEYS[0]], ids=["current", "retired"])
def model_key(request) -> str:
    """Run every case against both a current and a retired model key.

    Nothing in DetectionSet reads the key, so both runs assert the same thing:
    reading and saving a campaign's detections does not depend on which model
    wrote them. That is exactly what keeps a pre-upgrade campaign loadable, so
    it is worth asserting rather than assuming.

    Parametrizing also keeps the key out of the test bodies, so these cases
    cannot go stale the way a hardcoded "BirdNET-2.4" did.
    """
    return request.param


def _campaign_dir(project_folder: Path, campaign: str) -> Path:
    folder = project_folder / campaign
    folder.mkdir(parents=True, exist_ok=True)
    campaign_toml(folder).write_text("", encoding="utf-8")
    return folder


def _write_csv(folder: Path, model_key: str, rows: list[list[str]], headers: list[str] | None = None) -> Path:
    path = campaign_csv_for_model(folder, model_key)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers or _HEADERS)
        w.writerows(rows)
    return path


def _seed_csv(project_folder: Path, campaign: str, rows: list[list[str]], model_key: str) -> Path:
    folder = _campaign_dir(project_folder, campaign)
    _write_csv(folder, model_key, rows)
    return folder


def test_load_for_campaign_parses_numeric_and_annotation_columns(tmp_path: Path, model_key: str) -> None:
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
        model_key,
    )
    detections = DetectionSet.load_for_campaign(folder).detections
    assert len(detections) == 2
    assert detections[0].confidence == 0.85
    assert detections[0].verified == VerifiedState.TRUE
    assert detections[1].corrected_species == "Magpie"
    assert detections[1].comment == "uncertain id"


def test_load_prefixes_file_with_campaign_folder_name(tmp_path: Path, model_key: str) -> None:
    """On disk File is campaign-relative; in memory it is project-relative."""
    folder = _seed_csv(tmp_path, "east", [_sample("east")], model_key)
    detections = DetectionSet.load_for_campaign(folder).detections
    assert detections[0].file == "east/f.wav"


def test_load_combined_concatenates_campaign_csvs(tmp_path: Path, model_key: str) -> None:
    _seed_csv(tmp_path, "east", [_sample("east")], model_key)
    _seed_csv(tmp_path, "west", [_sample("west")], model_key)
    detections = DetectionSet.load_combined(tmp_path).detections
    assert {d.campaign for d in detections} == {"east", "west"}


def test_load_combined_reads_several_models_in_one_campaign(tmp_path: Path) -> None:
    """A campaign analyzed before and after a model change loads both CSVs.

    Not parametrized: the point here is precisely that the keys differ, which
    is what a campaign carrying a pre-upgrade run looks like on disk.
    """
    folder = _campaign_dir(tmp_path, "east")
    _write_csv(folder, DEFAULT_MODEL_KEY, [_sample("east")])
    for retired in RETIRED_MODEL_KEYS:
        _write_csv(folder, retired, [_sample("east")])

    detections = DetectionSet.load_for_campaign(folder).detections
    assert len(detections) == 1 + len(RETIRED_MODEL_KEYS)


def test_load_combined_skips_non_campaign_dirs(tmp_path: Path, model_key: str) -> None:
    _seed_csv(tmp_path, "east", [_sample("east")], model_key)
    stray = tmp_path / "not-a-campaign"
    stray.mkdir()
    _write_csv(stray, model_key, [_sample("stray")])
    detections = DetectionSet.load_combined(tmp_path).detections
    assert {d.campaign for d in detections} == {"east"}


def test_save_round_trip_preserves_edits(tmp_path: Path, model_key: str) -> None:
    folder = _seed_csv(tmp_path, "east", [_sample("east")], model_key)
    ds = DetectionSet.load_for_campaign(folder)
    ds.detections[0].verified = VerifiedState.TRUE
    ds.detections[0].comment = "edited"
    ds.save()

    reloaded = DetectionSet.load_for_campaign(folder).detections
    assert reloaded[0].verified == VerifiedState.TRUE
    assert reloaded[0].comment == "edited"


def test_save_keeps_file_campaign_relative_on_disk(tmp_path: Path, model_key: str) -> None:
    """Saving an edit must not leak the in-memory campaign prefix to disk."""
    folder = _seed_csv(tmp_path, "east", [_sample("east")], model_key)
    ds = DetectionSet.load_for_campaign(folder)
    ds.detections[0].comment = "edited"
    ds.save()

    with open(campaign_csv_for_model(folder, model_key), encoding="utf-8") as f:
        row = next(csv.DictReader(f))
    assert row["File"] == "f.wav"
    # The in-memory detection stays project-relative even after the save.
    assert ds.detections[0].file == "east/f.wav"


def test_save_failure_leaves_original_file_intact(tmp_path: Path, model_key: str, monkeypatch) -> None:
    """A crash mid-write must not truncate the CSV holding user annotations.

    The write goes to a '.part' sibling that is swapped in atomically, so a
    serialization failure leaves the original bytes untouched and no temp
    file behind.
    """
    folder = _seed_csv(tmp_path, "east", [_sample("east")], model_key)
    csv_path = campaign_csv_for_model(folder, model_key)
    original_bytes = csv_path.read_bytes()

    ds = DetectionSet.load_for_campaign(folder)

    def _boom(_d):
        raise RuntimeError("simulated crash mid-serialization")

    monkeypatch.setattr("pam_analyzer.domain.detection_set.schema.detection_to_row", _boom)
    try:
        ds.save()
    except RuntimeError:
        pass

    assert csv_path.read_bytes() == original_bytes
    assert not list(csv_path.parent.glob("*.part"))


def test_save_leaves_no_temp_file(tmp_path: Path, model_key: str) -> None:
    folder = _seed_csv(tmp_path, "east", [_sample("east")], model_key)
    DetectionSet.load_for_campaign(folder).save()
    csv_path = campaign_csv_for_model(folder, model_key)
    assert not list(csv_path.parent.glob("*.part"))


def test_lat_lon_round_trip(tmp_path: Path, model_key: str) -> None:
    """Lat/Lon are core fields that map to named Detection attributes, not extra."""
    folder = _campaign_dir(tmp_path, "east")
    path = _write_csv(
        folder, model_key, [_sample("east") + ["48.0", "11.0"]], headers=_HEADERS + ["Lat", "Lon"]
    )

    ds = DetectionSet.load_for_campaign(folder)
    detections = ds.detections
    assert detections[0].lat == 48.0
    assert detections[0].lon == 11.0
    assert "Lat" not in detections[0].extra
    assert "Lon" not in detections[0].extra

    ds.save()
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        row = next(reader)
    assert row["Lat"] == "48"
    assert row["Lon"] == "11"


def test_truly_unknown_columns_go_to_extra(tmp_path: Path, model_key: str) -> None:
    """Columns outside detection_schema.CORE_FIELDS still land in Detection.extra."""
    folder = _campaign_dir(tmp_path, "east")
    _write_csv(folder, model_key, [_sample("east") + ["mytag"]], headers=_HEADERS + ["CustomTag"])

    detections = DetectionSet.load_for_campaign(folder).detections
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
