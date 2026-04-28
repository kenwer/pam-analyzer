"""Audio snippet extraction backed by soundfile."""

from pathlib import Path

import soundfile as sf


class SoundfileAudioExtractor:
    def __init__(self) -> None:
        self._duration_cache: dict[Path, float] = {}

    def duration(self, src: Path) -> float:
        cached = self._duration_cache.get(src)
        if cached is not None:
            return cached
        try:
            value = sf.info(src).duration
        except Exception:
            value = 0.0
        self._duration_cache[src] = value
        return value

    def extract(self, src: Path, start: float, end: float, dst: Path) -> None:
        audio, sr = sf.read(src)
        end = min(len(audio) / sr, end)
        snippet = audio[int(start * sr) : int(end * sr)]
        dst.parent.mkdir(parents=True, exist_ok=True)
        sf.write(dst, snippet, sr)
