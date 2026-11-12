"""Which mounted volumes PsutilSdCardScanner offers as cards."""

import sys
from collections import namedtuple
from pathlib import Path

import pytest

from pam_analyzer.infrastructure import psutil_sdcard_scanner
from pam_analyzer.infrastructure.psutil_sdcard_scanner import PsutilSdCardScanner

Partition = namedtuple("Partition", "device mountpoint")

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="labels come from the mount path off Windows")


@pytest.fixture
def mounted(monkeypatch: pytest.MonkeyPatch) -> list[Partition]:
    partitions: list[Partition] = []
    monkeypatch.setattr(psutil_sdcard_scanner.psutil, "disk_partitions", lambda: partitions)
    return partitions


def test_matching_is_case_insensitive_and_strips_a_padded_label(mounted: list[Partition]) -> None:
    """Song Meter pads its FAT label, and the label becomes a folder name Windows rejects with trailing spaces."""
    mounted += [
        Partition("/dev/disk4s1", "/Volumes/msd-109"),
        Partition("/dev/disk5s1", "/Volumes/2MM30692   "),
        Partition("/dev/disk1s1", "/"),
        Partition("/dev/disk2s1", "/Volumes/Backup"),
    ]

    cards = PsutilSdCardScanner().scan("^(MSD-|2MM)")

    assert [(c.name, c.device) for c in cards] == [("msd-109", "/dev/disk4s1"), ("2MM30692", "/dev/disk5s1")]
    assert cards[1].mountpoint == Path("/Volumes/2MM30692   ")


def test_a_pattern_still_being_typed_matches_nothing(mounted: list[Partition]) -> None:
    mounted.append(Partition("/dev/disk4s1", "/Volumes/MSD-109"))
    assert PsutilSdCardScanner().scan("^(MSD-") == []


def test_a_volume_whose_label_cannot_be_read_does_not_hide_the_others(
    mounted: list[Partition], monkeypatch: pytest.MonkeyPatch
) -> None:
    mounted += [Partition("/dev/disk3s1", "/Volumes/MSD-BAD"), Partition("/dev/disk4s1", "/Volumes/MSD-109")]
    real = psutil_sdcard_scanner._get_volume_name

    def flaky(partition: Partition) -> str | None:
        if partition.mountpoint.endswith("BAD"):
            raise OSError("device not ready")
        return real(partition)

    monkeypatch.setattr(psutil_sdcard_scanner, "_get_volume_name", flaky)

    assert [c.name for c in PsutilSdCardScanner().scan("^MSD-")] == ["MSD-109"]
