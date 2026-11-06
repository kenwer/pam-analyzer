"""DetectionsTableModel: the spread-key cap."""

import csv
from pathlib import Path

import pytest

from pam_analyzer.domain import DetectionStore, SpreadKey
from pam_analyzer.domain.filter_ops import FilterOp
from pam_analyzer.ui.models.detections_table_model import DetectionsTableModel

# (ARU, Species, Confidence, Recording_Time). Robin at MSD-1 has three strong
# hits on 04-25 and a weaker one on each of the two following days.
_ROWS = [
    ("MSD-1", "Robin", 0.97, "2026-04-25T04:00:00"),
    ("MSD-1", "Robin", 0.95, "2026-04-25T04:10:00"),
    ("MSD-1", "Robin", 0.93, "2026-04-25T04:20:00"),
    ("MSD-1", "Robin", 0.61, "2026-04-26T05:00:00"),
    ("MSD-1", "Robin", 0.44, "2026-04-27T05:00:00"),
    ("MSD-2", "Robin", 0.50, "2026-04-25T04:00:00"),
    ("MSD-1", "Crow", 0.80, "2026-04-25T06:00:00"),
]


@pytest.fixture
def model(qtbot, tmp_path: Path) -> DetectionsTableModel:
    path = tmp_path / "detections-test.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Campaign", "ARU", "Species", "Confidence", "Start_Time", "End_Time", "File", "Recording_Time"])
        for aru, species, conf, rec in _ROWS:
            w.writerow(["c", aru, species, conf, "0.0", "3.0", "f.wav", rec])
    m = DetectionsTableModel()
    m.set_store(DetectionStore.from_paths([path]))
    return m


def _column(model: DetectionsTableModel, name: str) -> list:
    col = model.index_of(name)
    return [model.data(model.index(r, col)) for r in range(model.rowCount())]


def test_default_cap_is_per_aru_species(model: DetectionsTableModel) -> None:
    model.set_max_per(1)
    assert _column(model, "Confidence") == [0.97, 0.5, 0.8]


def test_max_per_caps_by_spread_groups(model: DetectionsTableModel) -> None:
    model.set_spread({SpreadKey.ARU, SpreadKey.DAY})
    model.set_max_per(1)
    assert _column(model, "Confidence") == [0.97, 0.61, 0.44, 0.5, 0.8]


def test_spread_without_aru_ranks_study_wide(model: DetectionsTableModel) -> None:
    model.set_spread(set())
    model.set_max_per(1)
    assert _column(model, "Confidence") == [0.97, 0.8]


def test_cap_ranks_after_column_filters(model: DetectionsTableModel) -> None:
    model.set_column_filter(model.index_of("Confidence"), "0.9", FilterOp.LESS_THAN)
    model.set_max_per(1)
    assert _column(model, "Confidence") == [0.61, 0.5, 0.8]
