import csv
from pathlib import Path

import polars as pl
import pytest

from pam_analyzer.domain import Detection, DetectionStore, VerifiedState
from pam_analyzer.domain import detection_schema as schema
from pam_analyzer.domain import detection_store as detection_store_module
from pam_analyzer.domain.detection_schema import campaign_csv_for_model
from pam_analyzer.domain.detection_store import (
    empty_detections_frame,
    read_detections_csv,
    write_detections_csv,
    write_detections_frame,
)
from pam_analyzer.domain.paths import campaign_toml
from tests.conftest import DEFAULT_MODEL_KEY, RETIRED_MODEL_KEYS

_HEADER = (
    "Campaign,ARU,Start_Time,End_Time,Scientific_Name,Species,Species_de,Confidence,"
    "Rank,File,Recording_Time,Week,Model,Verified,Comment"
)
_ROWS = [
    "east,MSD-1,0.0,3.0,Erithacus rubecula,Robin,Rotkehlchen,0.85,1,MSD-1/f.wav,2026-04-25T08:00:00,24,BirdNET-2.4,,",
    'east,MSD-1, 3.0 ,6.0,Parus major,"Tit, Great",,0.5,,MSD-1/g.wav,2026-04-25 08:00,-1,BirdNET-2.4,true,"a, b"',
    'east,MSD-2,6.0,9.0,Corvus corone,Crow,Krähe,abc,x,/abs/x.wav,,,Perch-2.0,uncertain,"two\nlines"',
    "east,MSD-2,9.0,12.0,Corvus corone,Crow,Krähe,0.3,2,,,24,Perch-2.0,false,",
]


def _write_raw(folder: Path, rows: list[str], header: str = _HEADER) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "detections-BirdNET-2.4.csv"
    path.write_text(header + "\r\n" + "\r\n".join(rows) + "\r\n", encoding="utf-8", newline="")
    return path


def _csv_module_read(path: Path) -> list[Detection]:
    """The csv-module reader this replaces, kept as the parsing oracle."""
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out = []
    for row in rows:
        d = schema.detection_from_row(row)
        if d.file and not Path(d.file).is_absolute():
            d.file = f"{path.parent.name}/{d.file}"
        out.append(d)
    return out


def test_read_matches_csv_module_reader(tmp_path: Path) -> None:
    path = _write_raw(tmp_path / "east", _ROWS)
    frame, fieldnames = read_detections_csv(path)
    assert fieldnames == _HEADER.split(",")
    records = [schema.detection_from_record(r) for r in frame.iter_rows(named=True)]
    assert records == _csv_module_read(path)


def test_read_types_numeric_columns(tmp_path: Path) -> None:
    frame, _ = read_detections_csv(_write_raw(tmp_path / "east", _ROWS))
    assert frame.schema["Confidence"] == pl.Float64
    assert frame["Confidence"].to_list() == [0.85, 0.5, 0.0, 0.3]
    assert frame["Rank"].to_list() == [1.0, None, None, 2.0]
    # Schema columns the file lacks are present, so every store has the same core columns.
    assert frame["Lat"].to_list() == [None] * 4
    assert frame["Corrected_Species"].to_list() == [""] * 4


def test_read_short_row_and_bom_header(tmp_path: Path) -> None:
    # The csv-module reader turned the missing cells into the text "None".
    path = _write_raw(tmp_path / "east", ["east,MSD-1,0.0"], header="﻿Campaign,ARU,Start_Time,Species")
    frame, fieldnames = read_detections_csv(path)
    d = schema.detection_from_record(frame.row(0, named=True))
    assert fieldnames[0] == "Campaign"
    assert (d.campaign, d.species, d.confidence, d.rank) == ("east", "", 0.0, None)


def test_read_rejects_unknown_verified_value(tmp_path: Path) -> None:
    path = _write_raw(tmp_path / "east", ["east,MSD-1,0,3,S,s,,0.5,1,f.wav,,24,M,TRUE,"])
    with pytest.raises(ValueError, match="TRUE"):
        read_detections_csv(path)


def test_read_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "east" / "detections-BirdNET-2.4.csv"
    path.parent.mkdir()
    path.write_text("", encoding="utf-8")
    frame, fieldnames = read_detections_csv(path)
    assert fieldnames == []
    assert frame.height == 0
    assert frame.schema == empty_detections_frame().schema


_HEADERS = [
    "Campaign", "ARU", "Week", "Species", "Scientific_Name", "Confidence", "Start_Time",
    "End_Time", "Rank", "File", "Recording_Time", "Verified", "Corrected_Species", "Comment",
]


@pytest.fixture(params=[DEFAULT_MODEL_KEY, RETIRED_MODEL_KEYS[0]], ids=["current", "retired"])
def model_key(request) -> str:
    """Loading and saving must not depend on which model wrote the CSV."""
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


def _sample(campaign: str, **overrides: str) -> list[str]:
    row = dict(zip(_HEADERS, [
        campaign, "MSD-1", "24", "Robin", "Erithacus rubecula", "0.85", "0.0", "3.0",
        "1", "f.wav", "2026-04-25T08:00:00", "", "", "",
    ], strict=True))
    row.update(overrides)
    return [row[h] for h in _HEADERS]


def _rows(store: DetectionStore):
    return [store.detection(i) for i in range(store.row_count)]


def test_load_parses_numeric_and_annotation_columns(tmp_path: Path, model_key: str) -> None:
    folder = _seed_csv(tmp_path, "east", [
        _sample("east", Verified="true"),
        _sample("east", Species="Crow", Confidence="0.5", Corrected_Species="Magpie", Comment="uncertain id"),
    ], model_key)
    rows = _rows(DetectionStore.load_for_campaign(folder))
    assert len(rows) == 2
    assert rows[0].confidence == 0.85
    assert rows[0].verified is VerifiedState.TRUE
    assert rows[1].corrected_species == "Magpie"
    assert rows[1].comment == "uncertain id"


def test_load_prefixes_file_with_campaign_folder_name(tmp_path: Path, model_key: str) -> None:
    folder = _seed_csv(tmp_path, "east", [_sample("east")], model_key)
    assert DetectionStore.load_for_campaign(folder).detection(0).file == "east/f.wav"


def test_load_combined_concatenates_campaign_csvs(tmp_path: Path, model_key: str) -> None:
    _seed_csv(tmp_path, "east", [_sample("east")], model_key)
    _seed_csv(tmp_path, "west", [_sample("west")], model_key)
    store = DetectionStore.load_combined(tmp_path)
    assert {d.campaign for d in _rows(store)} == {"east", "west"}


def test_load_reads_several_models_in_one_campaign(tmp_path: Path) -> None:
    folder = _campaign_dir(tmp_path, "east")
    _write_csv(folder, DEFAULT_MODEL_KEY, [_sample("east")])
    for retired in RETIRED_MODEL_KEYS:
        _write_csv(folder, retired, [_sample("east")])
    assert DetectionStore.load_for_campaign(folder).row_count == 1 + len(RETIRED_MODEL_KEYS)


def test_load_combined_skips_non_campaign_dirs(tmp_path: Path, model_key: str) -> None:
    _seed_csv(tmp_path, "east", [_sample("east")], model_key)
    stray = tmp_path / "not-a-campaign"
    stray.mkdir()
    _write_csv(stray, model_key, [_sample("stray")])
    assert {d.campaign for d in _rows(DetectionStore.load_combined(tmp_path))} == {"east"}


def test_unknown_columns_go_to_extra(tmp_path: Path, model_key: str) -> None:
    folder = _campaign_dir(tmp_path, "east")
    _write_csv(folder, model_key, [_sample("east") + ["mytag"]], headers=_HEADERS + ["CustomTag"])
    assert DetectionStore.load_for_campaign(folder).detection(0).extra == {"CustomTag": "mytag"}


def test_extra_column_of_one_file_is_absent_from_another_files_rows(tmp_path: Path, model_key: str) -> None:
    east = _campaign_dir(tmp_path, "east")
    _write_csv(east, model_key, [_sample("east") + ["mytag"]], headers=_HEADERS + ["CustomTag"])
    _seed_csv(tmp_path, "west", [_sample("west")], model_key)
    store = DetectionStore.load_combined(tmp_path)
    west_row = next(d for d in _rows(store) if d.campaign == "west")
    assert west_row.extra == {}


def test_set_annotation_round_trip(tmp_path: Path, model_key: str) -> None:
    folder = _seed_csv(tmp_path, "east", [_sample("east")], model_key)
    store = DetectionStore.load_for_campaign(folder)
    store.set_annotation(0, "Verified", "true")
    store.set_annotation(0, "Comment", "edited")
    assert store.dirty_count == 1
    assert store.save_dirty() == 1
    assert store.dirty_count == 0
    reloaded = DetectionStore.load_for_campaign(folder).detection(0)
    assert reloaded.verified is VerifiedState.TRUE
    assert reloaded.comment == "edited"


def test_set_annotation_rejects_read_only_column_and_bad_value(tmp_path: Path, model_key: str) -> None:
    store = DetectionStore.load_for_campaign(_seed_csv(tmp_path, "east", [_sample("east")], model_key))
    with pytest.raises(ValueError):
        store.set_annotation(0, "Species", "Crow")
    with pytest.raises(ValueError):
        store.set_annotation(0, "Verified", "maybe")
    assert store.dirty_count == 0


def test_set_annotations_edits_many_rows_at_once(tmp_path: Path, model_key: str) -> None:
    folder = _seed_csv(tmp_path, "east", [_sample("east"), _sample("east"), _sample("east")], model_key)
    store = DetectionStore.load_for_campaign(folder)
    store.set_annotations([0, 2], "Verified", "true")
    assert store.frame["Verified"].to_list() == ["true", "", "true"]
    assert store.dirty_count == 2
    assert store.save_dirty() == 2
    assert [d.verified for d in _rows(DetectionStore.load_for_campaign(folder))] == [
        VerifiedState.TRUE,
        VerifiedState.UNSET,
        VerifiedState.TRUE,
    ]


def test_set_annotations_rejects_bad_input_without_editing(tmp_path: Path, model_key: str) -> None:
    store = DetectionStore.load_for_campaign(_seed_csv(tmp_path, "east", [_sample("east")], model_key))
    with pytest.raises(ValueError):
        store.set_annotations([0], "Species", "Crow")
    with pytest.raises(ValueError):
        store.set_annotations([0], "Verified", "maybe")
    assert store.dirty_count == 0


def test_edit_does_not_change_an_earlier_frame(tmp_path: Path, model_key: str) -> None:
    store = DetectionStore.load_for_campaign(_seed_csv(tmp_path, "east", [_sample("east")], model_key))
    before = store.frame
    store.set_annotation(0, "Comment", "edited")
    assert before["Comment"][0] == ""
    assert store.frame["Comment"][0] == "edited"


def test_save_dirty_rewrites_only_the_owning_file(tmp_path: Path, model_key: str, monkeypatch) -> None:
    """One edit on a large combined set must not rewrite every campaign CSV."""
    east = _seed_csv(tmp_path, "east", [_sample("east"), _sample("east")], model_key)
    _seed_csv(tmp_path, "west", [_sample("west")], model_key)
    store = DetectionStore.load_combined(tmp_path)
    east_ids = [i for i, d in enumerate(_rows(store)) if d.campaign == "east"]
    # Two edits in the same file must still write it once.
    for i in east_ids:
        store.set_annotation(i, "Verified", "true")

    written: list[Path] = []
    real_write = detection_store_module.write_detections_frame
    monkeypatch.setattr(
        detection_store_module,
        "write_detections_frame",
        lambda path, frame, fields: (written.append(path), real_write(path, frame, fields)),
    )
    assert store.save_dirty() == 2
    assert written == [campaign_csv_for_model(east, model_key)]


def test_failed_save_keeps_rows_dirty(tmp_path: Path, model_key: str, monkeypatch) -> None:
    store = DetectionStore.load_for_campaign(_seed_csv(tmp_path, "east", [_sample("east")], model_key))
    store.set_annotation(0, "Comment", "edited")

    def _unplugged(*_args, **_kwargs):
        raise OSError("drive unplugged")

    monkeypatch.setattr(detection_store_module, "write_detections_frame", _unplugged)
    with pytest.raises(OSError):
        store.save_dirty()
    assert store.dirty_count == 1

    monkeypatch.undo()
    assert store.save_dirty() == 1


def test_save_keeps_file_campaign_relative_on_disk(tmp_path: Path, model_key: str) -> None:
    folder = _seed_csv(tmp_path, "east", [_sample("east")], model_key)
    store = DetectionStore.load_for_campaign(folder)
    store.set_annotation(0, "Comment", "edited")
    store.save_dirty()
    with open(campaign_csv_for_model(folder, model_key), encoding="utf-8") as f:
        assert next(csv.DictReader(f))["File"] == "f.wav"
    assert store.detection(0).file == "east/f.wav"


def test_save_without_edits_changes_nothing(tmp_path: Path, model_key: str) -> None:
    """After a first save settles the format, loading and saving again must not touch a byte."""
    folder = _seed_csv(tmp_path, "east", [_sample("east")], model_key)
    path = campaign_csv_for_model(folder, model_key)
    DetectionStore.load_for_campaign(folder).save()
    settled = path.read_bytes()
    DetectionStore.load_for_campaign(folder).save()
    assert path.read_bytes() == settled


def test_store_and_list_writer_write_the_same_bytes(tmp_path: Path, model_key: str) -> None:
    """The runner writes Detection lists and the store writes frames: one format for both."""
    folder = _seed_csv(tmp_path, "east", [_sample("east"), _sample("east", Comment="a, b")], model_key)
    store = DetectionStore.load_for_campaign(folder)
    rows = [store.detection(i) for i in range(store.row_count)]
    fieldnames = store.files[0].fieldnames
    from_frame, from_list = folder / "from_frame.csv", folder / "from_list.csv"

    write_detections_frame(from_frame, store.frame, fieldnames)
    write_detections_csv(from_list, rows, fieldnames)

    assert from_frame.read_bytes() == from_list.read_bytes()


def test_lat_lon_round_trip(tmp_path: Path, model_key: str) -> None:
    folder = _campaign_dir(tmp_path, "east")
    path = _write_csv(folder, model_key, [_sample("east") + ["48.0", "11.0"]], headers=_HEADERS + ["Lat", "Lon"])
    store = DetectionStore.load_for_campaign(folder)
    d = store.detection(0)
    assert (d.lat, d.lon) == (48.0, 11.0)
    assert "Lat" not in d.extra
    store.save()
    with open(path, encoding="utf-8") as f:
        row = next(csv.DictReader(f))
    assert (row["Lat"], row["Lon"]) == ("48.0", "11.0")


def test_save_leaves_no_temp_file(tmp_path: Path, model_key: str) -> None:
    folder = _seed_csv(tmp_path, "east", [_sample("east")], model_key)
    DetectionStore.load_for_campaign(folder).save()
    assert not list(folder.glob("*.part"))


def test_empty_store() -> None:
    store = DetectionStore.empty()
    assert store.row_count == 0
    assert store.save_dirty() == 0
    store.save()


def test_text_needing_quotes_round_trips(tmp_path: Path, model_key: str) -> None:
    awkward = _sample("east", Species="Tit, Blue", Comment='said "hi"\nnext line', Corrected_Species=" padded ")
    folder = _seed_csv(tmp_path, "east", [awkward], model_key)
    DetectionStore.load_for_campaign(folder).save()
    d = DetectionStore.load_for_campaign(folder).detection(0)
    assert (d.species, d.comment, d.corrected_species) == ("Tit, Blue", 'said "hi"\nnext line', " padded ")


def _detection(file: str = "east/f.wav") -> Detection:
    return Detection(
        campaign="east", aru="MSD-1", week=24.0, species="Robin",
        scientific_name="Erithacus rubecula", confidence=0.85, start_time=0.0,
        end_time=3.0, rank=1.0, file=file,
    )


def test_write_detections_csv_strips_only_this_campaigns_prefix(tmp_path: Path) -> None:
    path = tmp_path / "east" / "detections-X.csv"
    write_detections_csv(path, [_detection("east/a.wav"), _detection("other/x.wav")], list(schema.COLUMN_NAMES))
    with open(path, newline="", encoding="utf-8") as f:
        assert [r["File"] for r in csv.DictReader(f)] == ["a.wav", "other/x.wav"]


def test_write_detections_csv_writes_every_header_column_and_blank_cells_unquoted(tmp_path: Path) -> None:
    path = tmp_path / "east" / "detections-X.csv"
    write_detections_csv(path, [_detection()], ["Campaign", "File", "Unused"])
    assert path.read_bytes() == (
        b"Campaign,File,Unused,Verified,Corrected_Species,Comment\r\n"
        b"east,f.wav,,,,\r\n"
    )


def test_write_detections_csv_without_rows_writes_header_only(tmp_path: Path) -> None:
    path = tmp_path / "east" / "detections-X.csv"
    write_detections_csv(path, [], ["Campaign", "Unused"])
    assert path.read_bytes() == b"Campaign,Unused,Verified,Corrected_Species,Comment\r\n"


def test_write_detections_csv_merges_a_duplicated_header_column(tmp_path: Path) -> None:
    """A hand-edited header naming a column twice saves as one column, not a DuplicateError."""
    path = tmp_path / "east" / "detections-X.csv"
    write_detections_csv(path, [_detection()], ["Campaign", "Comment", "Comment"])
    assert path.read_bytes().splitlines()[0] == b"Campaign,Comment,Verified,Corrected_Species"


def test_load_combined_frame_is_one_chunk_per_column(tmp_path: Path, model_key: str) -> None:
    """Per-file saves filter the frame, which is slow over thousands of read chunks."""
    _seed_csv(tmp_path, "east", [_sample("east")], model_key)
    _seed_csv(tmp_path, "west", [_sample("west")], model_key)
    frame = DetectionStore.load_combined(tmp_path).frame
    assert frame.n_chunks("all") == [1] * frame.width
