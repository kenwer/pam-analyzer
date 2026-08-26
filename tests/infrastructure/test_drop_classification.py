"""How the row loop classifies a detection the species filter drops.

Two counters are logged apart so a legacy-name mismatch does not hide behind
ordinary geography: out-of-region (a class the model knows, just not expected
here) and not-on-axis (a name the model cannot emit at all). Telling them
apart needs the axis of the model that actually ran, which for PerchRunner is
not the BirdNET v3.0 axis it borrows for geo lookups and locale labels.

The stub here drives the real _run_campaign row loop with a fake session, so
no model is loaded and nothing is downloaded.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from pam_analyzer.domain import AnalysisSettings, Campaign, FilterMode
from pam_analyzer.infrastructure.base_analysis_runner import BaseAnalysisRunner, ParsedRow

# The model's own axis is wider than the one it borrows for geo and locales,
# which is exactly PerchRunner's situation.
_BORROWED_AXIS = frozenset({"Allowed bird", "Known bird"})
_OWN_AXIS = _BORROWED_AXIS | {"Sound event"}


class _FakeTaxonomy:
    def locale_label_map(self, lang: str) -> dict[str, str]:
        return {}

    def known_species_scientific(self) -> frozenset[str]:
        return _BORROWED_AXIS

    def region_species_scientific(self, lat: float, lon: float, week: int) -> frozenset[str]:
        return frozenset()

    def available_locales(self) -> tuple[str, ...]:
        return ("en_us",)


class _FakeResult:
    def __init__(self, rows: list[str]) -> None:
        self._rows = rows

    def to_structured_array(self) -> list[dict[str, Any]]:
        return [{"species_name": name} for name in self._rows]


class _FakeSession:
    def __init__(self, rows: list[str]) -> None:
        self._rows = rows

    def run(self, wav_files: list[Path]) -> _FakeResult:
        return _FakeResult(self._rows)


class _SilentProgress:
    def report(self, snapshot: Any) -> None:
        pass

    def is_cancelled(self) -> bool:
        return False


class _AxisStubRunner(BaseAnalysisRunner):
    """Runs the real row loop over a fixed list of species names."""

    model_key = "Stub-1.0"
    log_prefix = "stub"
    taxonomy = _FakeTaxonomy()  # type: ignore[assignment]

    def __init__(self, rows: list[str]) -> None:
        self._rows = rows

    def _known_output_species(self) -> frozenset[str]:
        return _OWN_AXIS

    def _load_model(self) -> Any:
        return object()

    @contextmanager  # type: ignore[arg-type]
    def _open_predict_session(self, model: Any, **kwargs: Any) -> Any:
        yield _FakeSession(self._rows)

    def _parse_row(self, raw_row: Any, **kwargs: Any) -> ParsedRow:
        name = str(raw_row["species_name"])
        return ParsedRow(
            file_path=Path("a.wav"),
            start_time=0.0,
            end_time=3.0,
            scientific_name=name,
            match_name=name,
            confidence=0.9,
            preferred_common=name,
            locale_commons={},
        )


def _drop_counts(caplog: pytest.LogCaptureFixture) -> tuple[int, int]:
    """(out-of-region, not-on-axis) as the run's drop summary reported them."""
    for record in caplog.records:
        if "species filter dropped" in str(record.msg):
            _, _total, out_of_region, not_on_axis, _kept = record.args  # type: ignore[misc]
            return int(out_of_region), int(not_on_axis)
    raise AssertionError("no drop-summary log record was emitted")


def test_the_two_drop_reasons_are_counted_apart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """'Sound event' is on the running model's axis but not on the borrowed
    one, so it is an ordinary filter drop rather than an unknown name. Only
    'Typo bird', on neither axis, is the not-on-axis case. Both are asserted
    because an axis that wrongly reported everything as emittable would still
    satisfy the first count on its own.
    """
    (tmp_path / "a.wav").write_bytes(b"")
    (tmp_path / "species_list.txt").write_text("Allowed bird\n", encoding="utf-8")

    runner = _AxisStubRunner(["Allowed bird", "Known bird", "Sound event", "Typo bird"])
    campaign = Campaign(name="c1", folder=tmp_path, species_filter_mode=FilterMode.LIST)

    with caplog.at_level(logging.INFO):
        runner.run(
            campaigns=[campaign],
            settings=AnalysisSettings(min_conf=0.1, overlap=0.0, locales=()),
            preferred_lang="en_us",
            progress=_SilentProgress(),
        )

    assert _drop_counts(caplog) == (2, 1)
