"""The result of one analysis run: how it ended and what it produced.

A AnalysisRunResult is the transient event of one run invocation. It is distinct
from AnalysisInventory (in `inventory`), which is the persistent on-disk view
of every result discovery finds, independent of any single run. They share
CampaignResult as their element but answer different questions ("how did the
run go?" vs "what results exist?").
"""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CampaignResult:
    campaign_name: str
    output_dir: Path
    detections_csv: Path
    species_list_txt: Path | None  # location mode only
    detection_count: int
    wav_count: int
    aru_count: int
    elapsed: float
    warnings: tuple[str, ...] = ()
    model_key: str = "" # Model that produced this row (allows the BirdNET panel to add hints)


class RunStatus(Enum):
    """How an analysis run ended.

    A run over several campaigns writes each campaign's CSV before moving on,
    so the campaigns that finished are real work no matter how the run ends.
    COMPLETED ran the whole list, CANCELLED stopped on a user request, FAILED
    stopped on an error (see AnalysisRunResult.error).
    """

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AnalysisRunResult:
    """The result of one run invocation: how it ended plus what it produced.

    `campaigns` holds every campaign that finished, even when `status` is
    CANCELLED or FAILED, so a partial run never loses the work already on
    disk. `error` carries the failure message when status is FAILED.
    """

    status: RunStatus
    campaigns: tuple[CampaignResult, ...] = ()
    elapsed: float = 0.0
    error: str | None = None
