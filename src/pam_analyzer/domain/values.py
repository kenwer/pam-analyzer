from dataclasses import dataclass


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
    """User-facing BirdNET run parameters."""

    min_conf: float = 0.25
    overlap: float = 0.0
    locales: tuple[str, ...] = ()  # frozen for hashability
