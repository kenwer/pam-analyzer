"""Tests for extract_snippet."""

from pathlib import Path

import numpy as np
import soundfile as sf

from pam_analyzer.infrastructure import extract_snippet

SR = 8000


def _write_source(path: Path, subtype: str) -> np.ndarray:
    rng = np.random.default_rng(0)
    audio = (rng.integers(-2000, 2000, SR * 3) / 32768).astype("float32")
    sf.write(path, audio, SR, subtype=subtype)
    return audio


def test_extract_writes_bit_exact_flac_snippet(tmp_path: Path) -> None:
    src = tmp_path / "rec.flac"
    _write_source(src, "PCM_16")
    dst = tmp_path / "out" / "snip.flac"

    extract_snippet(src, 0.5, 1.25, dst)

    info = sf.info(dst)
    assert (info.format, info.subtype) == ("FLAC", "PCM_16")
    expected, _ = sf.read(src, dtype="int16")
    actual, _ = sf.read(dst, dtype="int16")
    np.testing.assert_array_equal(actual, expected[int(0.5 * SR) : int(1.25 * SR)])


def test_extract_clamps_end_to_file_length(tmp_path: Path) -> None:
    src = tmp_path / "rec.flac"
    _write_source(src, "PCM_16")
    dst = tmp_path / "snip.flac"

    extract_snippet(src, 2.5, 10.0, dst)

    assert sf.info(dst).frames == int(0.5 * SR)


def test_extract_float_wav_source_becomes_pcm_24(tmp_path: Path) -> None:
    src = tmp_path / "rec.wav"
    audio = _write_source(src, "FLOAT")
    dst = tmp_path / "snip.flac"

    extract_snippet(src, 0.0, 1.0, dst)

    assert sf.info(dst).subtype == "PCM_24"
    actual, _ = sf.read(dst, dtype="float32")
    np.testing.assert_allclose(actual, audio[:SR], atol=2**-23)
