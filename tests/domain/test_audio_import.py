"""Tests for domain/audio_import.py: birdnet_week boundaries and CardQueue behaviour."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pam_analyzer.domain.audio_import import (
    CardQueue,
    DetectedCard,
    ImportSource,
    TimeSource,
    birdnet_week,
    date_range_from_stems,
    discover_folder_cards,
    format_recording_time,
    parse_recording_time,
    resolve_recording_time,
    same_original_name,
)

# birdnet_week

@pytest.mark.parametrize(
    "month, day, expected",
    [
        (1, 1, 1),    # Jan 1 -> week 1
        (1, 7, 1),    # Jan 7 -> week 1
        (1, 8, 2),    # Jan 8 -> week 2
        (1, 31, 5),   # Jan 31 -> week 5 (ceil(31/7)=5)
        (3, 1, 9),    # Mar 1 -> (2)*4+1 = 9
        (12, 31, 48), # Dec 31 -> min(48, 44+5) = 48
        (12, 1, 45),  # Dec 1 -> 44+1 = 45
    ],
)
def test_birdnet_week_boundaries(month, day, expected):
    dt = datetime(2024, month, day)
    assert birdnet_week(dt) == expected


def test_birdnet_week_capped_at_48():
    dt = datetime(2024, 12, 31)
    assert birdnet_week(dt) <= 48


# parse_recording_time / date_range_from_stems

def test_parse_recording_time_extracts_stamp():
    assert parse_recording_time("ARU1_20240501_063000") == datetime(2024, 5, 1, 6, 30, 0)


def test_parse_recording_time_skips_digits_of_a_serial_prefix():
    # The serial's trailing digits plus the date also look like YYYYMMDD_HHMMSS.
    assert parse_recording_time("248D9B0164876836_20260520_075600") == datetime(2026, 5, 20, 7, 56, 0)


def test_parse_recording_time_returns_none_without_stamp():
    assert parse_recording_time("no_timestamp_here") is None
    assert parse_recording_time("20241345_990000") is None  # invalid month/time


# resolve_recording_time

_CEST = timezone(timedelta(hours=2))
_STEM = "242A26056487DFA3_20250508_222946"


def test_resolve_prefers_guano_and_drops_its_offset_without_converting():
    resolved = resolve_recording_time(datetime(2025, 5, 8, 22, 29, 46, tzinfo=_CEST), _STEM)

    assert resolved.time == datetime(2025, 5, 8, 22, 29, 46)
    assert resolved.time.tzinfo is None
    assert resolved.source is TimeSource.GUANO
    assert not resolved.filename_conflict


def test_resolve_flags_guano_and_filename_wall_clocks_that_differ():
    resolved = resolve_recording_time(datetime(2025, 5, 8, 23, 29, 46, tzinfo=_CEST), _STEM)

    assert resolved.time == datetime(2025, 5, 8, 23, 29, 46)  # GUANO still wins
    assert resolved.filename_conflict


def test_resolve_falls_back_to_filename_then_mtime():
    start = datetime(2025, 5, 8, 22, 0, 0)

    from_name = resolve_recording_time(None, _STEM, start)
    from_mtime = resolve_recording_time(None, "no_stamp", start)

    assert (from_name.time, from_name.source) == (datetime(2025, 5, 8, 22, 29, 46), TimeSource.FILENAME)
    assert (from_mtime.time, from_mtime.source) == (start, TimeSource.MTIME)


def test_resolve_returns_none_without_any_source():
    assert resolve_recording_time(None, "no_stamp") is None


def test_format_recording_time_keeps_the_csv_format():
    assert format_recording_time(datetime(2025, 5, 8, 22, 29, 46, 500_000)) == "2025-05-08 22:29:46"
    assert format_recording_time(None) == ""


@pytest.mark.parametrize(
    "original, name, same",
    [
        ("242A_20250508_222946.WAV", "242A_20250508_222946.WAV", True),
        ("242A_20250508_222946.WAV", "242A_20250508_222946.flac", True),  # imported earlier
        (r"D:\card\242A_20250508_222946.WAV", "242a_20250508_222946.wav", True),
        ("242A_20250508_222946.WAV", "renamed.WAV", False),
    ],
)
def test_same_original_name_compares_stems_case_insensitively(original, name, same):
    assert same_original_name(original, name) is same


def test_date_range_from_stems_returns_min_max():
    stems = ["a_20240630_180000", "b_20240501_060000", "not-a-stamp"]
    assert date_range_from_stems(stems) == (
        datetime(2024, 5, 1, 6, 0, 0),
        datetime(2024, 6, 30, 18, 0, 0),
    )


def test_date_range_from_stems_none_when_nothing_parses():
    assert date_range_from_stems(["junk", "also-junk"]) is None
    assert date_range_from_stems([]) is None


# CardQueue

def _card(name: str) -> DetectedCard:
    return DetectedCard(name=name, mountpoint=Path("/mnt") / name, device=f"/dev/{name}")


def test_card_queue_offer_and_pop():
    q = CardQueue()
    q.offer([_card("A"), _card("B")])
    assert q.pop() == _card("A")
    assert q.pop() == _card("B")
    assert q.pop() is None


def test_card_queue_dedup():
    q = CardQueue()
    q.offer([_card("A"), _card("A")])
    assert q.pop() == _card("A")
    assert q.pop() is None


def test_card_queue_seen_across_offers():
    q = CardQueue()
    q.offer([_card("A")])
    q.pop()
    q.offer([_card("A")])  # already seen, should not be re-added
    assert q.pop() is None


def test_card_queue_clear_seen_allows_requeue():
    q = CardQueue()
    q.offer([_card("A")])
    q.pop()
    q.clear_seen()
    q.offer([_card("A")])
    assert q.pop() == _card("A")


def test_card_queue_reset():
    q = CardQueue()
    q.offer([_card("A"), _card("B")])
    q.reset()
    assert q.pending == []
    q.offer([_card("A")])  # should work after reset
    assert len(q.pending) == 1


def test_card_queue_pending_does_not_mutate():
    q = CardQueue()
    q.offer([_card("A")])
    snapshot = q.pending
    snapshot.clear()
    assert len(q.pending) == 1


# discover_folder_cards

def test_discover_folder_cards_single_card(tmp_path):
    root = tmp_path / "MSD-12345"
    root.mkdir()

    cards = discover_folder_cards(root, lambda folder: folder == root)
    assert len(cards) == 1
    assert cards[0].name == "MSD-12345"
    assert cards[0].mountpoint == root
    assert cards[0].source is ImportSource.FOLDER


def test_discover_folder_cards_batch_subfolders(tmp_path):
    root = tmp_path / "OldRecordings"
    root.mkdir()
    card_a = root / "CardA"
    card_a.mkdir()
    card_b = root / "CardB"
    card_b.mkdir()

    cards = discover_folder_cards(root, lambda folder: folder in (card_a, card_b))
    assert {c.name for c in cards} == {"CardA", "CardB"}
    assert all(c.source is ImportSource.FOLDER for c in cards)


def test_discover_folder_cards_skips_empty_subfolders(tmp_path):
    root = tmp_path / "Mixed"
    root.mkdir()
    has_audio = root / "HasAudio"
    has_audio.mkdir()
    (root / "Empty").mkdir()

    cards = discover_folder_cards(root, lambda folder: folder == has_audio)
    assert [c.name for c in cards] == ["HasAudio"]


def test_discover_folder_cards_none_found(tmp_path):
    root = tmp_path / "Empty"
    root.mkdir()
    (root / "Sub").mkdir()

    cards = discover_folder_cards(root, lambda _folder: False)
    assert cards == []


def test_discover_folder_cards_skips_stray_files(tmp_path):
    root = tmp_path / "WithStrayFile"
    root.mkdir()
    (root / "notes.txt").write_text("hello")

    cards = discover_folder_cards(root, lambda _folder: False)
    assert cards == []
