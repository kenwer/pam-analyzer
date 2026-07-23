from dataclasses import dataclass

# Largest overlap (seconds) any model accepts. BirdNET's analyze.core caps
# overlap at 2.5 s on its 3 s window; Perch would allow up to 4.9 s on its 5 s
# window, but the setting is now project-wide and model-agnostic, so we apply
# the more conservative BirdNET limit to every run.
MAX_OVERLAP_S = 2.5


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

    min_conf: float = 0.25
    overlap: float = 0.0
    locales: tuple[str, ...] = ()  # frozen for hashability
