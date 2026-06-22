"""Tests for infrastructure/audio_import.py transcoding helpers.

Covers transcode_to_flac: lossless round-trip, the non-PCM_16 fallback signal,
and that a failed verify leaves no output behind.
"""

import shutil
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from pam_analyzer.domain.audio_import import ConflictChoice, DetectedCard
from pam_analyzer.infrastructure import audio_import
from pam_analyzer.infrastructure.audio_import import (
    AudioImporter,
    NotLosslessError,
    VerifyError,
    _flac_target_name,
    transcode_to_flac,
)

SR = 48_000
STAMP = "20260619_073000"  # parses to a recording time, so week bucketing works


def _write_wav(path: Path, audio: np.ndarray, subtype: str = "PCM_16") -> None:
    sf.write(path, audio, SR, subtype=subtype)


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
