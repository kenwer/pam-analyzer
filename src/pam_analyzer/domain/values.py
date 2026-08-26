from dataclasses import dataclass

# Largest overlap (seconds) the models accept. Both BirdNET v2.4 and v3.0
# frame audio in 3 s windows, so consecutive windows must advance by at
# least 0.5 s.
MAX_OVERLAP_S = 2.5
DEFAULT_MIN_CONF = 0.25

# Model key a new project starts on. A literal rather than an import from
# the runner, because the domain layer must not depend on infrastructure.
# The panel falls back to its first runner if a project names a key that no
# longer ships, so this string going stale degrades gracefully.
DEFAULT_ANALYSIS_MODEL = "BirdNET-2.4"

# Scientific-name axis a new project writes its detections under. Also a
# literal, for the same layering reason, and matching the axis identifiers
# in infrastructure.legacy_names.TAXONOMIES. A project naming an axis this
# build no longer offers leaves names un-normalized rather than failing.
DEFAULT_TAXONOMY = "BirdNET-3.0"

# Species-name language a new project starts on, and the value a project
# naming a language this build does not offer is corrected to. en_us is the
# one code every model ships, so it is always selectable.
DEFAULT_SPECIES_LANG = "en_us"

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
    canonical_taxonomy: str = DEFAULT_TAXONOMY  # axis every model's output is written under
