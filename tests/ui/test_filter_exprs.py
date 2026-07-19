"""Parity tests pinning to_polars_expr to the pure-Python matches().

The table model filters through polars, the unit tests reason through
matches(); these tests assert both implementations keep the same rows
for every operator, including null and unparseable cells.
"""

import polars as pl
import pytest

from pam_analyzer.domain.filter_ops import ColumnKind, FilterOp, matches
from pam_analyzer.ui.models.filter_exprs import datetime_helper_exprs, to_polars_expr

TEXT_VALUES = ["Turdus merula", "Erithacus rubecula", "MSD-109", "", None]

NUMERIC_VALUES = [0.1, 0.85, 0.9, 3.0, None]

DATETIME_VALUES = [
    "2026-04-25 03:00:00",
    "2026-04-25T08:00:00",
    "2026-04-26 23:30:00",
    "2026-05-02 12:00",
    "garbage",
    "",
    None,
]

CATEGORICAL_VALUES = ["MSD-109", "msd-110", "MSD-111", "", None]


CASES = [
    (ColumnKind.TEXT, TEXT_VALUES, "merula", FilterOp.CONTAINS),
    (ColumnKind.TEXT, TEXT_VALUES, "merula", FilterOp.NOT_CONTAINS),
    (ColumnKind.TEXT, TEXT_VALUES, "turdus merula", FilterOp.EQUALS),
    (ColumnKind.TEXT, TEXT_VALUES, "turdus merula", FilterOp.NOT_EQUALS),
    (ColumnKind.TEXT, TEXT_VALUES, "erit", FilterOp.STARTS_WITH),
    (ColumnKind.TEXT, TEXT_VALUES, "109", FilterOp.ENDS_WITH),
    (ColumnKind.TEXT, TEXT_VALUES, "", FilterOp.BLANK),
    (ColumnKind.TEXT, TEXT_VALUES, "", FilterOp.NOT_BLANK),
    (ColumnKind.NUMERIC, NUMERIC_VALUES, "0.85", FilterOp.EQUALS),
    (ColumnKind.NUMERIC, NUMERIC_VALUES, "0.85", FilterOp.NOT_EQUALS),
    (ColumnKind.NUMERIC, NUMERIC_VALUES, "0.5", FilterOp.GREATER_THAN),
    (ColumnKind.NUMERIC, NUMERIC_VALUES, "0.9", FilterOp.LESS_THAN_OR_EQUAL),
    (ColumnKind.NUMERIC, NUMERIC_VALUES, "0.2 - 1", FilterOp.IN_RANGE),
    # Unparseable filter text is inactive; null cells must survive too.
    (ColumnKind.NUMERIC, NUMERIC_VALUES, "abc", FilterOp.EQUALS),
    # Text-style op on a numeric column compares the stringified value.
    (ColumnKind.NUMERIC, NUMERIC_VALUES, "0.8", FilterOp.CONTAINS),
    (ColumnKind.NUMERIC, NUMERIC_VALUES, "", FilterOp.BLANK),
    (ColumnKind.DATETIME, DATETIME_VALUES, "2026-04-25", FilterOp.ON_DATE),
    (ColumnKind.DATETIME, DATETIME_VALUES, "2026-04-26", FilterOp.BEFORE_DATE),
    (ColumnKind.DATETIME, DATETIME_VALUES, "2026-04-25", FilterOp.AFTER_DATE),
    (ColumnKind.DATETIME, DATETIME_VALUES, "2026-04-25 .. 2026-04-26", FilterOp.DATE_RANGE),
    (ColumnKind.DATETIME, DATETIME_VALUES, "03:00 - 09:00", FilterOp.TIME_OF_DAY_RANGE),
    # Overnight window wrapping midnight; null cells must stay excluded.
    (ColumnKind.DATETIME, DATETIME_VALUES, "22:00 - 04:00", FilterOp.TIME_OF_DAY_RANGE),
    # Mid-typing filter text is inactive; garbage cells must survive.
    (ColumnKind.DATETIME, DATETIME_VALUES, "2026-0", FilterOp.ON_DATE),
    (ColumnKind.DATETIME, DATETIME_VALUES, "04:0", FilterOp.TIME_OF_DAY_RANGE),
    # Plain text ops still work on the raw datetime string.
    (ColumnKind.DATETIME, DATETIME_VALUES, "2026-04", FilterOp.CONTAINS),
    (ColumnKind.DATETIME, DATETIME_VALUES, "", FilterOp.BLANK),
    (ColumnKind.CATEGORICAL, CATEGORICAL_VALUES, "MSD-109; msd-111", FilterOp.IS_ANY_OF),
    (ColumnKind.CATEGORICAL, CATEGORICAL_VALUES, "MSD-10", FilterOp.IS_ANY_OF),
    (ColumnKind.CATEGORICAL, CATEGORICAL_VALUES, ";;", FilterOp.IS_ANY_OF),
    (ColumnKind.CATEGORICAL, CATEGORICAL_VALUES, "msd-110", FilterOp.EQUALS),
]


@pytest.mark.parametrize(
    "kind, values, text, op",
    CASES,
    ids=[f"{kind.value}-{op.value}-{text!r}" for kind, values, text, op in CASES],
)
def test_polars_expr_matches_python_matcher(kind, values, text, op):
    df = pl.DataFrame({"col": values}, strict=False)
    if kind is ColumnKind.DATETIME:
        df = df.with_columns(datetime_helper_exprs("col"))
    kept = df.with_row_index("idx").filter(to_polars_expr("col", text, op, kind))["idx"].to_list()
    expected = [i for i, v in enumerate(values) if matches(v, text, op, kind)]
    assert kept == expected
