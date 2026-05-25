"""Ports and value objects for the BirdNET analysis boundary.

AnalysisRunner is the structural seam for analysis: BirdnetRunner
implements it for production, and tests supply a FakeRunner that satisfies
the protocol structurally (duck typing).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .entities import AnalysisRunResult
from .enums import FilterMode
from .values import AnalysisSettings, LatLon


class CancelledError(Exception):
    """Raised by AnalysisRunner when progress.is_cancelled() flips to True."""


@dataclass(frozen=True, slots=True)
class AnalysisProgressSnapshot:
    """A single progress update from the runner.

    phase is one of: 'preparing', 'analyzing', 'parsing', 'done'.
    files_total may be 0 before counting completes.
    phase_detail carries an optional extra string (e.g. current file basename)
    that the UI may render alongside the phase.
    """

    campaign: str
    campaign_index: int  # 1-based
    total_campaigns: int
    files_done: int
    files_total: int
    phase: str
    phase_detail: str | None = None


class AnalysisProgress(Protocol):
    def report(self, snapshot: AnalysisProgressSnapshot) -> None: ...
    def is_cancelled(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class CampaignRunInput:
    name: str
    folder: Path
    mode: FilterMode
    location: LatLon | None
    species_list_text: str | None  # LIST mode only
    # LOCATION mode only: species merged on top of the location-derived list.
    must_have_species_text: str | None = None


class AnalysisRunner(Protocol):
    # Short filesystem-safe identifier for this runner. Used as a suffix
    # on output CSV filenames so multiple model runs can coexist in one
    # campaign directory, and as the per-row Model column value so the
    # repo can route edits back to the right file.
    model_key: str

    def count_audio_files(self, campaign_dir: Path) -> int: ...
    def available_locales(self) -> list[str]: ...

    def run(
        self,
        *,
        campaigns: list[CampaignRunInput],
        output_base: Path,
        settings: AnalysisSettings,
        preferred_lang: str,
        audio_root: Path,
        progress: AnalysisProgress,
    ) -> AnalysisRunResult: ...
