from enum import StrEnum


class FilterMode(StrEnum):
    """How a campaign restricts BirdNET's species search space."""

    LOCATION = "location"
    LIST = "list"


class VerifiedState(StrEnum):
    """User verification of a detection. Empty string maps to UNSET."""

    UNSET = ""
    TRUE = "true"
    FALSE = "false"
    UNCERTAIN = "uncertain"
