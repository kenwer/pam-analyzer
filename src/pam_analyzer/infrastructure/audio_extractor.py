"""Audio snippet extraction backed by soundfile."""

from pathlib import Path

import soundfile as sf


def extract_snippet(src: Path, start: float, end: float, dst: Path) -> None:
    """Write the *start*..*end* seconds of *src* to *dst* as FLAC.

    Only the snippet's frames are decoded, and a stop past the end of the
    file is clamped by soundfile. FLAC has no float subtype, so float
    sources are written as 24-bit PCM.
    """
    info = sf.info(src)
    sr = info.samplerate
    snippet, _ = sf.read(src, start=int(start * sr), stop=int(end * sr), dtype="float32")
    subtype = info.subtype if sf.check_format("FLAC", info.subtype) else "PCM_24"
    dst.parent.mkdir(parents=True, exist_ok=True)
    sf.write(dst, snippet, sr, format="FLAC", subtype=subtype)
