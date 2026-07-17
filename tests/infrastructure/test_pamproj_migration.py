"""Tests for the one-time migration of legacy standalone .pamproj projects."""

import csv
from pathlib import Path

import pytest

from pam_analyzer.infrastructure import (
    TomlProjectRepository,
    find_legacy_pamproj,
    load_legacy,
    migrate,
    paths,
)

_CSV_HEADER = "Campaign,Species,Confidence,File,Verified,CustomTag\n"


def _make_legacy_project(
    tmp_path: Path,
    *,
    explicit_output: bool = True,
    campaigns: tuple[str, ...] = ("alpha",),
) -> tuple[Path, Path, Path]:
    """Build a legacy layout: returns (pamproj_path, audio_root, output_base)."""
    audio_root = tmp_path / "audio"
    output_base = (tmp_path / "out") if explicit_output else (audio_root / "demo-detections")
    pamproj = tmp_path / "demo.pamproj"

    for name in campaigns:
        campaign = audio_root / name
        campaign.mkdir(parents=True)
        paths.campaign_toml(campaign).write_text('species_filter_mode = "location"\n')
        out_dir = output_base / name
        out_dir.mkdir(parents=True)
        (out_dir / f"{name}-detections-BirdNET-2.4.csv").write_text(
            _CSV_HEADER + f"{name},Robin,0.9,{name}/MSD-1/week_08/r.flac,true,keepme\n",
            encoding="utf-8",
        )
        (out_dir / f"{name}-species-list.txt").write_text("Robin\n", encoding="utf-8")
        (out_dir / f"{name}-species-list-week-08.txt").write_text("Robin\n", encoding="utf-8")

    out_line = f'detections_output_path = "{output_base}"\n' if explicit_output else ""
    pamproj.write_text(
        "[project]\n"
        f'audio_recordings_path = "{audio_root}"\n'
        + out_line
        + 'sdcard_name_pattern = "^FIELD-"\n'
        + "birdnet_min_conf = 0.4\n",
        encoding="utf-8",
    )
    return pamproj, audio_root, output_base


def test_migrate_happy_path(tmp_path: Path) -> None:
    pamproj, audio_root, output_base = _make_legacy_project(tmp_path)

    report = migrate(load_legacy(pamproj))

    assert report.project_folder == audio_root
    assert report.moved_csvs == 1
    assert report.warnings == ()

    # CSV moved, renamed, File column stripped of the campaign prefix,
    # unknown columns preserved.
    dest = audio_root / "alpha" / "detections-BirdNET-2.4.csv"
    with open(dest, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["File"] == "MSD-1/week_08/r.flac"
    assert rows[0]["Verified"] == "true"
    assert rows[0]["CustomTag"] == "keepme"

    # Species lists renamed into the campaign folder.
    assert (audio_root / "alpha" / "applied-species-list.txt").exists()
    assert (audio_root / "alpha" / "applied-species-list-week-08.txt").exists()

    # Settings carried over into pam-analyzer.toml. Legacy file kept as .bak.
    project = TomlProjectRepository().load(audio_root)
    assert project.sdcard_name_pattern == "^FIELD-"
    assert project.birdnet_min_conf == 0.4
    assert not pamproj.exists()
    assert pamproj.with_name("demo.pamproj.bak").exists()

    # Emptied legacy output tree removed.
    assert not output_base.exists()


def test_migrate_skips_existing_destination_with_warning(tmp_path: Path) -> None:
    pamproj, audio_root, _ = _make_legacy_project(tmp_path)
    dest = audio_root / "alpha" / "detections-BirdNET-2.4.csv"
    dest.write_text("already here", encoding="utf-8")

    report = migrate(load_legacy(pamproj))

    assert report.moved_csvs == 0
    assert any("already exists" in w for w in report.warnings)
    assert dest.read_text(encoding="utf-8") == "already here"


def test_migrate_default_output_base_inside_audio_root(tmp_path: Path) -> None:
    """No explicit detections_output_path: the old default <audio>/<name>-detections."""
    pamproj, audio_root, output_base = _make_legacy_project(tmp_path, explicit_output=False)
    assert output_base.parent == audio_root

    report = migrate(load_legacy(pamproj))

    assert report.moved_csvs == 1
    assert (audio_root / "alpha" / "detections-BirdNET-2.4.csv").exists()
    assert not output_base.exists()


def test_migrate_output_base_equals_audio_root(tmp_path: Path) -> None:
    """Degenerate config: outputs written directly into the audio root.

    The move is then a rename within each campaign folder, and cleanup must
    not touch the audio root or any campaign folder.
    """
    audio_root = tmp_path / "audio"
    campaign = audio_root / "alpha"
    campaign.mkdir(parents=True)
    paths.campaign_toml(campaign).write_text('species_filter_mode = "location"\n')
    (campaign / "alpha-detections-BirdNET-2.4.csv").write_text(
        _CSV_HEADER + "alpha,Robin,0.9,alpha/MSD-1/r.flac,,x\n", encoding="utf-8"
    )
    pamproj = tmp_path / "demo.pamproj"
    pamproj.write_text(
        "[project]\n"
        f'audio_recordings_path = "{audio_root}"\n'
        f'detections_output_path = "{audio_root}"\n',
        encoding="utf-8",
    )

    report = migrate(load_legacy(pamproj))

    assert report.moved_csvs == 1
    assert (campaign / "detections-BirdNET-2.4.csv").exists()
    assert not (campaign / "alpha-detections-BirdNET-2.4.csv").exists()
    assert campaign.exists() and audio_root.exists()


def test_migrate_without_output_tree_still_writes_project_file(tmp_path: Path) -> None:
    audio_root = tmp_path / "audio"
    audio_root.mkdir()
    pamproj = tmp_path / "demo.pamproj"
    pamproj.write_text(
        f'[project]\naudio_recordings_path = "{audio_root}"\n', encoding="utf-8"
    )

    report = migrate(load_legacy(pamproj))

    assert report.moved_csvs == 0
    assert paths.project_toml(audio_root).exists()
    assert pamproj.with_name("demo.pamproj.bak").exists()


def test_load_legacy_rejects_missing_audio_root(tmp_path: Path) -> None:
    pamproj = tmp_path / "demo.pamproj"
    pamproj.write_text(
        '[project]\naudio_recordings_path = "/does/not/exist"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError):
        load_legacy(pamproj)

    pamproj.write_text("[project]\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_legacy(pamproj)


def test_migrate_is_rerunnable(tmp_path: Path) -> None:
    """Running migrate twice must not corrupt anything or move files back."""
    pamproj, audio_root, _ = _make_legacy_project(tmp_path)
    legacy = load_legacy(pamproj)
    migrate(legacy)

    report2 = migrate(legacy)

    assert report2.moved_csvs == 0
    dest = audio_root / "alpha" / "detections-BirdNET-2.4.csv"
    with open(dest, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["File"] == "MSD-1/week_08/r.flac"


def test_find_legacy_pamproj(tmp_path: Path) -> None:
    assert find_legacy_pamproj(tmp_path) is None

    legacy = tmp_path / "demo.pamproj"
    legacy.write_text("[project]\n", encoding="utf-8")
    assert find_legacy_pamproj(tmp_path) == legacy

    # pam-analyzer.toml is never a candidate.
    paths.project_toml(tmp_path).write_text("[project]\n", encoding="utf-8")
    assert find_legacy_pamproj(tmp_path) == legacy

    # Two legacy candidates are ambiguous.
    (tmp_path / "other.pamproj").write_text("[project]\n", encoding="utf-8")
    assert find_legacy_pamproj(tmp_path) is None
