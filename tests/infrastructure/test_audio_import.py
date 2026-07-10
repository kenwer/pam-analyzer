"""Tests for infrastructure/audio_import.py transcoding helpers.

Covers transcode_to_flac: lossless round-trip, the non-PCM_16 fallback signal,
that a failed verify leaves no output behind, and that GUANO metadata survives
the WAV-to-FLAC transcode (re-embedded as a 'GUANO' Vorbis comment).
"""

import shutil
from datetime import datetime
from pathlib import Path

import guano
import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC

from pam_analyzer.domain.audio_import import (
    ConflictChoice,
    DetectedCard,
    ImportSource,
    birdnet_week,
    discover_folder_cards,
)
from pam_analyzer.infrastructure import audio_import
from pam_analyzer.infrastructure.audio_import import (
    AudioImporter,
    NotLosslessError,
    VerifyError,
    _flac_target_name,
    extract_recording_time,
    read_guano,
    transcode_to_flac,
)

SR = 48_000
STAMP = "20260619_073000"  # parses to a recording time, so week bucketing works


def _write_wav(path: Path, audio: np.ndarray, subtype: str = "PCM_16") -> None:
    sf.write(path, audio, SR, subtype=subtype)


def _write_wav_with_guano(path: Path, audio: np.ndarray, **fields) -> None:
    """Write a PCM_16 WAV at *path* carrying a GUANO 'guan' chunk.

    fields are written into the GUANO block (e.g. Timestamp=..., Make=...).
    """
    _write_wav(path, audio)
    gf = guano.GuanoFile(str(path))
    gf["GUANO|Version"] = 1.0
    for key, value in fields.items():
        gf[key.replace("_", " ")] = value
    gf.write(make_backup=False)


def _pcm16(frames: int, channels: int = 1, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    shape = frames if channels == 1 else (frames, channels)
    return rng.integers(-32768, 32767, size=shape, dtype=np.int16)


def test_flac_target_name_replaces_extension():
    assert _flac_target_name(Path("20260619_073000.WAV")) == "20260619_073000.flac"
    assert _flac_target_name(Path("/cards/A/rec.wav")) == "rec.flac"


def test_transcode_is_lossless_mono(tmp_path: Path):
    src = tmp_path / "rec.wav"
    dst = tmp_path / "rec.flac"
    audio = _pcm16(SR)  # 1 second
    _write_wav(src, audio)

    frames = transcode_to_flac(src, dst)

    assert frames == len(audio)
    assert dst.exists()
    decoded, _ = sf.read(dst, dtype="int16")
    assert np.array_equal(decoded, audio)
    assert sf.info(dst).format == "FLAC"


def test_transcode_is_lossless_stereo(tmp_path: Path):
    src = tmp_path / "rec.wav"
    dst = tmp_path / "rec.flac"
    audio = _pcm16(SR, channels=2)
    _write_wav(src, audio)

    transcode_to_flac(src, dst)

    decoded, _ = sf.read(dst, dtype="int16")
    assert np.array_equal(decoded, audio)


def test_transcode_creates_missing_parent(tmp_path: Path):
    src = tmp_path / "rec.wav"
    dst = tmp_path / "week_25" / "rec.flac"
    _write_wav(src, _pcm16(SR // 10))

    transcode_to_flac(src, dst)

    assert dst.exists()


def test_non_pcm16_raises_not_lossless_and_writes_nothing(tmp_path: Path):
    src = tmp_path / "rec.wav"
    dst = tmp_path / "rec.flac"
    # 32-bit float source: no lossless FLAC representation via libsndfile.
    float_audio = np.linspace(-1.0, 1.0, SR, dtype=np.float32)
    _write_wav(src, float_audio, subtype="FLOAT")

    with pytest.raises(NotLosslessError) as exc:
        transcode_to_flac(src, dst)

    assert exc.value.subtype == "FLOAT"
    assert not dst.exists()


def test_verify_failure_leaves_no_output(tmp_path: Path, monkeypatch):
    src = tmp_path / "rec.wav"
    dst = tmp_path / "rec.flac"
    _write_wav(src, _pcm16(SR))

    # Force the round-trip comparison to fail, as a corrupt encode would.
    monkeypatch.setattr(audio_import.np, "array_equal", lambda *a, **k: False)

    with pytest.raises(VerifyError):
        transcode_to_flac(src, dst)

    assert not dst.exists()
    assert not (tmp_path / "rec.flac.part").exists()


# GUANO metadata preservation

def test_transcode_preserves_guano_metadata(tmp_path: Path):
    src = tmp_path / "rec.wav"
    dst = tmp_path / "rec.flac"
    ts = datetime(2026, 6, 19, 7, 30, 0)
    _write_wav_with_guano(src, _pcm16(SR // 10), Timestamp=ts, Make="Wildlife Acoustics")

    transcode_to_flac(src, dst)

    gf = read_guano(dst)
    assert gf is not None
    assert gf.get("Timestamp") == ts
    assert gf.get("Make") == "Wildlife Acoustics"
    # Stored under the canonical 'GUANO' Vorbis comment key.
    assert FLAC(str(dst)).get("GUANO") is not None


def test_transcode_without_guano_adds_no_comment(tmp_path: Path):
    src = tmp_path / "rec.wav"
    dst = tmp_path / "rec.flac"
    _write_wav(src, _pcm16(SR // 10))  # plain WAV, no 'guan' chunk

    transcode_to_flac(src, dst)

    assert read_guano(dst) is None
    assert FLAC(str(dst)).get("GUANO") is None


def test_read_guano_returns_none_for_plain_files(tmp_path: Path):
    wav = tmp_path / "plain.wav"
    flac = tmp_path / "plain.flac"
    _write_wav(wav, _pcm16(SR // 10))
    sf.write(flac, _pcm16(SR // 10), SR, subtype="PCM_16", format="FLAC")

    assert read_guano(wav) is None
    assert read_guano(flac) is None


def test_extract_recording_time_reads_guano_from_flac(tmp_path: Path):
    src = tmp_path / "rec.wav"
    flac = tmp_path / "rec.flac"
    ts = datetime(2026, 3, 1, 21, 5, 0)  # not derivable from this filename
    _write_wav_with_guano(src, _pcm16(SR // 10), Timestamp=ts)
    transcode_to_flac(src, flac)

    assert extract_recording_time(flac) == ts


def test_transcode_survives_guano_embed_failure(tmp_path: Path, monkeypatch):
    src = tmp_path / "rec.wav"
    dst = tmp_path / "rec.flac"
    _write_wav_with_guano(src, _pcm16(SR // 10), Timestamp=datetime(2026, 6, 19, 7, 30, 0))

    # Embedding runs after the verified FLAC is in place, so a metadata-write
    # failure (e.g. the Windows file lock this guards against) is best-effort:
    # the audio must survive and the transcode must still report success.
    def boom(*_a, **_k):
        raise OSError("file locked")

    monkeypatch.setattr(audio_import, "FLAC", boom)

    frames = transcode_to_flac(src, dst)

    assert frames == SR // 10
    assert dst.exists()
    decoded, _ = sf.read(dst, dtype="int16")
    assert len(decoded) == SR // 10


def test_import_preserves_guano_through_transcode(tmp_path: Path):
    card, dest = tmp_path / "card", tmp_path / "dest"
    card.mkdir()
    ts = datetime(2026, 6, 19, 7, 30, 0)
    src = card / f"{STAMP}.WAV"
    _write_wav_with_guano(src, _pcm16(SR // 5), Timestamp=ts, Make="AudioMoth")

    result = _run(AudioImporter(), _card(card), [src], dest)

    assert result.error == ""
    [flac] = _flacs(dest)
    gf = read_guano(flac)
    assert gf is not None
    assert gf.get("Timestamp") == ts
    assert gf.get("Make") == "AudioMoth"


# list_card_files

def test_list_card_files_includes_wav_flac_and_config(tmp_path: Path):
    card = tmp_path / "card"
    card.mkdir()
    (card / "a.WAV").touch()
    (card / "b.wav").touch()
    (card / "c.flac").touch()
    (card / "d.FLAC").touch()
    (card / "CONFIG.TXT").touch()
    (card / "notes.txt").touch()        # ignored
    (card / "sub").mkdir()              # ignored (not a file)

    names = [p.name for p in AudioImporter().list_card_files(card)]

    assert names == ["CONFIG.TXT", "a.WAV", "b.wav", "c.flac", "d.FLAC"]


def test_list_card_files_finds_audio_one_level_of_date_subfolders_deep(tmp_path: Path):
    """Some deployments group recordings into per-day folders under the device
    root (card_root/20260501/*.WAV) instead of at the root itself, e.g.:
    ID 125/CONFIG.TXT, ID 125/20260501/<device>_20260501_160000.WAV."""
    card = tmp_path / "ID 125"
    card.mkdir()
    (card / "CONFIG.TXT").touch()
    day = card / "20260501"
    day.mkdir()
    (day / "248D9B016487DF3C_20260501_160000.WAV").touch()
    (day / "248D9B016487DF3C_20260501_160400.WAV").touch()

    names = [p.name for p in AudioImporter().list_card_files(card)]

    assert names == [
        "248D9B016487DF3C_20260501_160000.WAV",
        "248D9B016487DF3C_20260501_160400.WAV",
        "CONFIG.TXT",
    ]


def test_list_card_files_does_not_recurse_past_one_level(tmp_path: Path):
    """A folder of several per-device cards (each with its own date subfolders)
    must not itself look like a single card -- otherwise discover_folder_cards'
    single-vs-batch check would flatten every device into one card."""
    site = tmp_path / "Bibersee"
    site.mkdir()
    device = site / "ID 125"
    device.mkdir()
    day = device / "20260501"
    day.mkdir()
    (day / "rec.WAV").touch()

    assert AudioImporter().list_card_files(site) == []
    assert [p.name for p in AudioImporter().list_card_files(device)] == ["rec.WAV"]


def test_discover_folder_cards_handles_device_with_date_subfolders(tmp_path: Path):
    """End-to-end regression for the site/device-id/date/*.WAV layout: the
    device-id folder holds only CONFIG.TXT directly, with recordings one level
    further down in a date-named subfolder."""
    site = tmp_path / "Bibersee"
    site.mkdir()
    device = site / "ID 125"
    device.mkdir()
    (device / "CONFIG.TXT").touch()
    day = device / "20260501"
    day.mkdir()
    (day / "248D9B016487DF3C_20260501_160000.WAV").touch()

    cards = discover_folder_cards(site, AudioImporter().has_direct_audio)

    assert [c.name for c in cards] == ["ID 125"]
    assert cards[0].source is ImportSource.FOLDER


def test_discover_folder_cards_distinguishes_batch_from_date_subfolders(tmp_path: Path):
    """A folder whose immediate children hold audio directly (no sidecar, no
    further nesting) is a batch of separate cards, not one card with date
    subfolders, even though 'root/child/*.WAV' is structurally identical to
    'device/date/*.WAV' by depth alone. has_direct_audio (not list_card_files,
    which would recurse into both children and merge them) is what tells them
    apart."""
    root = tmp_path / "OffloadedCards"
    root.mkdir()
    card_a = root / "CardA"
    card_a.mkdir()
    (card_a / "a.WAV").touch()
    card_b = root / "CardB"
    card_b.mkdir()
    (card_b / "b.WAV").touch()

    cards = discover_folder_cards(root, AudioImporter().has_direct_audio)

    assert {c.name for c in cards} == {"CardA", "CardB"}


def test_has_direct_audio_true_for_audio_at_root(tmp_path: Path):
    card = tmp_path / "card"
    card.mkdir()
    (card / "a.WAV").touch()
    assert AudioImporter().has_direct_audio(card)


def test_has_direct_audio_true_for_sidecar_only(tmp_path: Path):
    card = tmp_path / "card"
    card.mkdir()
    (card / "CONFIG.TXT").touch()
    assert AudioImporter().has_direct_audio(card)


def test_has_direct_audio_false_when_audio_only_in_subfolder(tmp_path: Path):
    card = tmp_path / "card"
    card.mkdir()
    day = card / "20260501"
    day.mkdir()
    (day / "a.WAV").touch()
    assert not AudioImporter().has_direct_audio(card)


# detect_conflicts

def _imported_flac(dest: Path, name: str) -> None:
    """Write a valid FLAC into dest under *name* (a finished prior import)."""
    sf.write(dest / name, _pcm16(SR // 10), SR, subtype="PCM_16", format="FLAC")


def test_conflicts_wav_with_decodable_flac_target_is_identical(tmp_path: Path):
    card, dest = tmp_path / "card", tmp_path / "dest"
    card.mkdir()
    dest.mkdir()
    _write_wav(card / "rec.WAV", _pcm16(SR // 10))
    _imported_flac(dest, "rec.flac")

    report = AudioImporter().detect_conflicts([card / "rec.WAV"], dest)

    assert report.identical == ("rec.WAV",)
    assert report.conflicts == ()


def test_conflicts_wav_with_no_target_is_neither(tmp_path: Path):
    card, dest = tmp_path / "card", tmp_path / "dest"
    card.mkdir()
    dest.mkdir()
    _write_wav(card / "rec.WAV", _pcm16(SR // 10))

    report = AudioImporter().detect_conflicts([card / "rec.WAV"], dest)

    assert report.identical == ()
    assert report.conflicts == ()


def test_conflicts_wav_with_stale_undecodable_flac_reimports(tmp_path: Path):
    card, dest = tmp_path / "card", tmp_path / "dest"
    card.mkdir()
    dest.mkdir()
    _write_wav(card / "rec.WAV", _pcm16(SR // 10))
    (dest / "rec.flac").write_bytes(b"not a real flac")  # leftover junk

    report = AudioImporter().detect_conflicts([card / "rec.WAV"], dest)

    # Not skipped (so the import overwrites it), and not a user conflict.
    assert report.identical == ()
    assert report.conflicts == ()


def test_conflicts_wav_with_legacy_bytecopy_is_identical(tmp_path: Path):
    card, dest = tmp_path / "card", tmp_path / "dest"
    card.mkdir()
    dest.mkdir()
    src = card / "rec.WAV"
    _write_wav(src, _pcm16(SR // 10))
    shutil.copy2(src, dest / "rec.WAV")  # pre-FLAC import preserved mtime

    report = AudioImporter().detect_conflicts([src], dest)

    assert report.identical == ("rec.WAV",)
    assert report.conflicts == ()


def test_conflicts_flac_passthrough_bytematch_is_identical(tmp_path: Path):
    card, dest = tmp_path / "card", tmp_path / "dest"
    card.mkdir()
    dest.mkdir()
    src = card / "rec.flac"
    sf.write(src, _pcm16(SR // 10), SR, subtype="PCM_16", format="FLAC")
    shutil.copy2(src, dest / "rec.flac")

    report = AudioImporter().detect_conflicts([src], dest)

    assert report.identical == ("rec.flac",)
    assert report.conflicts == ()


def test_conflicts_flac_passthrough_mismatch_is_conflict(tmp_path: Path):
    card, dest = tmp_path / "card", tmp_path / "dest"
    card.mkdir()
    dest.mkdir()
    src = card / "rec.flac"
    sf.write(src, _pcm16(SR // 10, seed=1), SR, subtype="PCM_16", format="FLAC")
    sf.write(dest / "rec.flac", _pcm16(SR, seed=2), SR, subtype="PCM_16", format="FLAC")

    report = AudioImporter().detect_conflicts([src], dest)

    assert report.identical == ()
    assert [c.filename for c in report.conflicts] == ["rec.flac"]


# import_card

def _card(mountpoint: Path) -> DetectedCard:
    return DetectedCard(name="A", mountpoint=mountpoint, device="/dev/A")


def _run(importer, card, files, dest, **kw):
    """Drive import_card with default callbacks; returns the CardImportResult."""
    return importer.import_card(
        card,
        files,
        dest,
        kw.get("resolutions", {}),
        kw.get("identical", ()),
        progress=kw.get("progress", lambda _p: None),
        is_cancelled=kw.get("is_cancelled", lambda: False),
        clear_after=kw.get("clear_after", False),
    )


def _flacs(dest: Path) -> list[Path]:
    return sorted(dest.rglob("*.flac"))


def test_import_transcodes_wav_to_flac_losslessly(tmp_path: Path):
    card, dest = tmp_path / "card", tmp_path / "dest"
    card.mkdir()
    audio = _pcm16(SR // 5)
    src = card / f"{STAMP}.WAV"
    _write_wav(src, audio)

    result = _run(AudioImporter(), _card(card), [src], dest)

    assert result.error == ""
    assert result.files_copied == 1
    assert result.files_skipped == 0
    flacs = _flacs(dest)
    assert [p.name for p in flacs] == [f"{STAMP}.flac"]
    decoded, _ = sf.read(flacs[0], dtype="int16")
    assert np.array_equal(decoded, audio)
    # Source byte count is reported, not the smaller FLAC.
    assert result.bytes_copied == src.stat().st_size


def test_import_handles_many_files(tmp_path: Path):
    card, dest = tmp_path / "card", tmp_path / "dest"
    card.mkdir()
    srcs = []
    for i in range(12):
        s = card / f"202606{10 + i:02d}_073000.WAV"  # unique days 10..21
        _write_wav(s, _pcm16(SR // 10, seed=i))
        srcs.append(s)

    result = _run(AudioImporter(), _card(card), srcs, dest)

    assert result.error == ""
    assert result.files_copied == 12
    assert len(_flacs(dest)) == 12


def test_import_flac_from_card_is_passthrough(tmp_path: Path):
    card, dest = tmp_path / "card", tmp_path / "dest"
    card.mkdir()
    audio = _pcm16(SR // 5)
    src = card / f"{STAMP}.flac"
    sf.write(src, audio, SR, subtype="PCM_16", format="FLAC")

    result = _run(AudioImporter(), _card(card), [src], dest)

    assert result.files_copied == 1
    copied = _flacs(dest)
    assert [p.name for p in copied] == [f"{STAMP}.flac"]
    decoded, _ = sf.read(copied[0], dtype="int16")
    assert np.array_equal(decoded, audio)


def test_import_config_txt_lands_at_dest_root(tmp_path: Path):
    card, dest = tmp_path / "card", tmp_path / "dest"
    card.mkdir()
    src = card / "CONFIG.TXT"
    src.write_text("gain=medium\n")

    result = _run(AudioImporter(), _card(card), [src], dest)

    assert result.files_copied == 1
    assert (dest / "CONFIG.TXT").read_text() == "gain=medium\n"


def test_import_non_pcm16_wav_falls_back_to_copy(tmp_path: Path):
    card, dest = tmp_path / "card", tmp_path / "dest"
    card.mkdir()
    src = card / f"{STAMP}.WAV"
    _write_wav(src, np.linspace(-1.0, 1.0, SR, dtype=np.float32), subtype="FLOAT")

    result = _run(AudioImporter(), _card(card), [src], dest)

    assert result.error == ""
    assert result.files_copied == 1
    assert _flacs(dest) == []                       # not transcoded
    wavs = sorted(dest.rglob("*.WAV"))
    assert [p.name for p in wavs] == [f"{STAMP}.WAV"]  # original preserved


@pytest.mark.parametrize("skip_kind", ["identical", "resolution"])
def test_import_skips_are_not_processed(tmp_path: Path, skip_kind: str):
    card, dest = tmp_path / "card", tmp_path / "dest"
    card.mkdir()
    src = card / f"{STAMP}.WAV"
    _write_wav(src, _pcm16(SR // 10))
    kw = (
        {"identical": (src.name,)}
        if skip_kind == "identical"
        else {"resolutions": {src.name: ConflictChoice.SKIP}}
    )

    result = _run(AudioImporter(), _card(card), [src], dest, **kw)

    assert result.files_skipped == 1
    assert result.files_copied == 0
    assert _flacs(dest) == []


def test_import_clear_after_deletes_sources_on_clean_run(tmp_path: Path):
    card, dest = tmp_path / "card", tmp_path / "dest"
    card.mkdir()
    src = card / f"{STAMP}.WAV"
    _write_wav(src, _pcm16(SR // 10))

    result = _run(AudioImporter(), _card(card), [src], dest, clear_after=True)

    assert result.error == ""
    assert not src.exists()         # cleared from card
    assert len(_flacs(dest)) == 1   # safely imported first


def test_import_clear_after_keeps_card_when_a_file_errors(tmp_path: Path):
    card, dest = tmp_path / "card", tmp_path / "dest"
    card.mkdir()
    good = card / f"{STAMP}.WAV"
    _write_wav(good, _pcm16(SR // 10))
    bad = card / "20260620_073000.WAV"
    bad.write_bytes(b"not audio")   # unreadable by soundfile

    result = _run(AudioImporter(), _card(card), [good, bad], dest, clear_after=True)

    assert result.error != ""       # the bad file surfaced an error
    assert good.exists()            # nothing cleared on a non-clean run
    assert bad.exists()


def test_import_cancellation_reports_and_keeps_card(tmp_path: Path):
    card, dest = tmp_path / "card", tmp_path / "dest"
    card.mkdir()
    src = card / f"{STAMP}.WAV"
    _write_wav(src, _pcm16(SR // 10))

    result = _run(
        AudioImporter(), _card(card), [src], dest,
        is_cancelled=lambda: True, clear_after=True,
    )

    assert result.error == "Cancelled"
    assert src.exists()             # never cleared on cancel


# Song Meter support: audio under Data/, <serial>_Summary.txt sidecar


def _songmeter_card(root: Path, serial: str = "2MM30692") -> Path:
    """Create a Song Meter card layout: a Data/ folder and a Summary at the root."""
    (root / "Data").mkdir(parents=True)
    (root / f"{serial}_Summary.txt").write_text("DATE,TIME,LAT,NS,LON,EW,POWER(V),#FILES\n")
    return root


def test_detect_profile_identifies_songmeter(tmp_path: Path):
    card = _songmeter_card(tmp_path / "2MM30692")
    assert audio_import.detect_profile(card) is audio_import.SONGMETER_PROFILE


def test_detect_profile_defaults_to_audiomoth(tmp_path: Path):
    card = tmp_path / "MSD-1"
    card.mkdir()
    (card / "CONFIG.TXT").touch()
    # A 'Data' dir alone is not enough without a *_Summary.txt next to it.
    (card / "Data").mkdir()
    assert audio_import.detect_profile(card) is audio_import.AUDIOMOTH_PROFILE


def test_list_card_files_songmeter_reads_data_dir_and_summary(tmp_path: Path):
    card = _songmeter_card(tmp_path / "2MM30692")
    (card / "Data" / "2MM30692_20260625_104904.wav").touch()
    (card / "Data" / "2MM30692_20260625_114902.wav").touch()
    (card / "Data" / "notes.txt").touch()   # ignored: not audio
    (card / "stray.wav").touch()            # ignored: audio not under Data/

    names = [p.name for p in AudioImporter().list_card_files(card)]

    assert names == [
        "2MM30692_20260625_104904.wav",
        "2MM30692_20260625_114902.wav",
        "2MM30692_Summary.txt",
    ]


def test_import_songmeter_buckets_by_guano_and_copies_summary(tmp_path: Path):
    card = _songmeter_card(tmp_path / "2MM30692")
    dest = tmp_path / "dest"
    ts = datetime(2026, 6, 25, 10, 49, 4)
    wav = card / "Data" / "2MM30692_20260625_104904.wav"
    _write_wav_with_guano(wav, _pcm16(SR // 5), Timestamp=ts, Make="Wildlife Acoustics")

    files = AudioImporter().list_card_files(card)
    result = _run(AudioImporter(), _card(card), files, dest)

    assert result.error == ""
    assert result.files_copied == 2                 # the WAV and the Summary
    # Bucketed by the GUANO timestamp (not the mtime fallback, not week 1).
    [flac] = _flacs(dest)
    assert flac.parent.name == f"week_{birdnet_week(ts):02d}"
    assert read_guano(flac).get("Timestamp") == ts  # metadata survived transcode
    assert (dest / "2MM30692_Summary.txt").exists()  # sidecar at the card-folder root


def test_import_songmeter_clear_after_removes_emptied_data_dir(tmp_path: Path):
    card = _songmeter_card(tmp_path / "2MM30692")
    dest = tmp_path / "dest"
    wav = card / "Data" / "2MM30692_20260625_104904.wav"
    _write_wav_with_guano(wav, _pcm16(SR // 10), Timestamp=datetime(2026, 6, 25, 10, 49, 4))
    summary = card / "2MM30692_Summary.txt"

    files = AudioImporter().list_card_files(card)
    result = _run(AudioImporter(), _card(card), files, dest, clear_after=True)

    assert result.error == ""
    assert not wav.exists()              # audio cleared
    assert not summary.exists()          # sidecar cleared
    assert not (card / "Data").exists()  # emptied Data/ removed
