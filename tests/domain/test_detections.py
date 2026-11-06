import polars as pl

from pam_analyzer.domain import SpreadKey
from pam_analyzer.domain.detections import detected_day_expr, group_rank_expr


def _frame(rows: list[tuple[str, str, float]], **extra: list) -> pl.DataFrame:
    aru, species, conf = zip(*rows, strict=True)
    return pl.DataFrame({"ARU": aru, "Species": species, "Confidence": conf, **extra})


def _ranks(frame: pl.DataFrame, *spread: SpreadKey) -> list[int]:
    if SpreadKey.DAY in spread:
        frame = frame.with_columns(detected_day_expr())
    return frame.select(group_rank_expr(frozenset(spread))).to_series().to_list()


def test_group_rank_orders_by_confidence_within_species() -> None:
    frame = _frame([("a", "Robin", 0.5), ("a", "Robin", 0.9), ("a", "Crow", 0.6), ("b", "Robin", 0.7)])
    # No spread keys: one group per species, across all ARUs.
    assert _ranks(frame) == [3, 1, 1, 2]


def test_group_rank_aru_key_ranks_each_aru_separately() -> None:
    frame = _frame([("a", "Robin", 0.5), ("a", "Robin", 0.9), ("a", "Crow", 0.6), ("b", "Robin", 0.7)])
    assert _ranks(frame, SpreadKey.ARU) == [2, 1, 1, 1]


def test_group_rank_breaks_ties_by_input_order() -> None:
    frame = _frame([("a", "s", 0.9), ("a", "s", 0.9), ("a", "s", 0.95)])
    assert _ranks(frame, SpreadKey.ARU) == [2, 3, 1]


def test_group_rank_model_key_ranks_each_model_separately() -> None:
    frame = _frame([("a", "s", 0.9), ("a", "s", 0.8), ("a", "s", 0.7)], Model=["BirdNET", "Perch", "BirdNET"])
    assert _ranks(frame, SpreadKey.MODEL) == [1, 1, 2]


def test_group_rank_week_key_reads_the_week_column() -> None:
    frame = _frame([("a", "s", 0.9), ("a", "s", 0.8), ("a", "s", 0.7)], Week=[17.0, 18.0, 17.0])
    assert _ranks(frame, SpreadKey.WEEK) == [1, 1, 2]


def test_group_rank_day_uses_detection_time_not_file_start() -> None:
    # Both rows come from one file that starts at 23:59:00 on 04-25. The second
    # detection is 120 s in, so it falls on 04-26 and gets its own day group.
    frame = _frame(
        [("a", "s", 0.9), ("a", "s", 0.8)],
        Recording_Time=["2026-04-25T23:59:00", "2026-04-25 23:59"],
        Start_Time=[0.0, 120.0],
    )
    assert _ranks(frame, SpreadKey.DAY) == [1, 1]


def test_group_rank_unparsable_time_forms_one_group() -> None:
    frame = _frame(
        [("a", "s", 0.9), ("a", "s", 0.8)], Recording_Time=["", "garbage"], Start_Time=[0.0, 0.0]
    )
    assert _ranks(frame, SpreadKey.DAY) == [1, 2]
