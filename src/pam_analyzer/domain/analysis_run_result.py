"""The result of one analysis run: how it ended and what it produced.

An AnalysisRunResult is the transient event of one run invocation. It is
distinct from AnalysisInventory (in `inventory`), the persistent on-disk view
of every result discovery finds, independent of any single run. The two answer
different questions ("how did the run go?" vs "what results exist?") and no
longer share an element type: a run produces CampaignRunResult, discovery
produces AnalysisInventoryEntry. The fields overlap only where the two views
genuinely agree (campaign_name, detections_csv, detection_count).
"""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CampaignRunResult:
    """One campaign's outcome within a single run.

    Every field here is run-event data the CLI summary reports: how many files
    and ARUs were seen, how long the campaign took, how many detections it
    produced, and any warnings. The paths that outlive the run (output_dir,
    species_list_txt) and the model tag are not carried here: only the on-disk
    view (AnalysisInventoryEntry) reads them, so keeping them here would just be
    write-only noise.
    """

    campaign_name: str
    detections_csv: Path
    detection_count: int
    wav_count: int
    aru_count: int
    elapsed: float
    warnings: tuple[str, ...] = ()


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
    campaigns: tuple[CampaignRunResult, ...] = ()
    elapsed: float = 0.0
    error: str | None = None
