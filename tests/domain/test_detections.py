import polars as pl

from pam_analyzer.domain import top_per_aru_species


def _frame(rows: list[tuple[str, str, float]]) -> pl.DataFrame:
    aru, species, conf = zip(*rows, strict=True)
    return pl.DataFrame({"ARU": aru, "Species": species, "Confidence": conf}).with_row_index("id")


def test_top_per_aru_species_keeps_highest_confidence() -> None:
    frame = _frame([
        ("ARU1", "Robin", 0.5), ("ARU1", "Robin", 0.9), ("ARU1", "Robin", 0.7),
        ("ARU1", "Crow", 0.6), ("ARU2", "Robin", 0.4),
    ])
    assert top_per_aru_species(frame, 2)["id"].to_list() == [1, 2, 3, 4]


def test_top_per_aru_species_breaks_ties_by_input_order() -> None:
    frame = _frame([("a", "s", 0.5), ("a", "s", 0.9), ("a", "s", 0.9), ("a", "s", 0.7)])
    assert top_per_aru_species(frame, 1)["id"].to_list() == [1]
    assert top_per_aru_species(frame, 2)["id"].to_list() == [1, 2]


def test_top_per_aru_species_disabled_returns_input() -> None:
    frame = _frame([("a", "s", 0.1)])
    assert top_per_aru_species(frame, 0) is frame
