from dataclasses import dataclass

# Largest overlap (seconds) the model accepts. BirdNET v3.0 frames audio in
# 3 s windows (96,000 samples at 32 kHz), so consecutive windows must advance
# by at least 0.5 s.
MAX_OVERLAP_S = 2.5
DEFAULT_MIN_CONF = 0.5

@dataclass(frozen=True, slots=True)
class LatLon:
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude <= 90.0:
            raise ValueError(f"latitude out of range: {self.latitude}")
        if not -180.0 <= self.longitude <= 180.0:
            raise ValueError(f"longitude out of range: {self.longitude}")


@dataclass(frozen=True, slots=True)
class AnalysisSettings:
    """Project-wide analysis run parameters, passed to any model runner."""

    min_conf: float = DEFAULT_MIN_CONF
    overlap: float = 0.0
    locales: tuple[str, ...] = ()  # frozen for hashability
