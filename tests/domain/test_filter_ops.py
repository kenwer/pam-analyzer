"""Unit tests for the operator matcher used by the header filter row."""

from datetime import date, time

import pytest

from pam_analyzer.domain.filter_ops import (
    ColumnKind,
    FilterOp,
    matches,
    parse_date,
    parse_date_range,
    parse_set_values,
    parse_time,
    parse_time_range,
)

# Text columns


@pytest.mark.parametrize(
    "value, text, op, expected",
    [
        ("Turdus merula", "merula", FilterOp.CONTAINS, True),
        ("Turdus merula", "MERULA", FilterOp.CONTAINS, True),
        ("Turdus merula", "robin", FilterOp.CONTAINS, False),
        ("Turdus merula", "robin", FilterOp.NOT_CONTAINS, True),
        ("Turdus merula", "merula", FilterOp.NOT_CONTAINS, False),
        ("MSD-109", "msd-109", FilterOp.EQUALS, True),
        ("MSD-109", "msd-110", FilterOp.EQUALS, False),
        ("MSD-109", "msd-110", FilterOp.NOT_EQUALS, True),
        ("Erithacus", "erit", FilterOp.STARTS_WITH, True),
        ("Erithacus", "thac", FilterOp.STARTS_WITH, False),
        ("Erithacus", "acus", FilterOp.ENDS_WITH, True),
        ("Erithacus", "erit", FilterOp.ENDS_WITH, False),
    ],
)
def test_text_ops(value, text, op, expected):
    assert matches(value, text, op, kind=ColumnKind.TEXT) is expected


# Numeric columns


@pytest.mark.parametrize(
    "value, text, op, expected",
    [
        (0.85, "0.8", FilterOp.GREATER_THAN, True),
        (0.85, "0.9", FilterOp.GREATER_THAN, False),
        (0.85, "0.85", FilterOp.GREATER_THAN_OR_EQUAL, True),
        (0.85, "0.85", FilterOp.LESS_THAN, False),
        (0.85, "0.86", FilterOp.LESS_THAN, True),
        (0.85, "0.85", FilterOp.LESS_THAN_OR_EQUAL, True),
        (0.85, "0.85", FilterOp.EQUALS, True),
        (0.85, "0.86", FilterOp.EQUALS, False),
        (0.85, "0.86", FilterOp.NOT_EQUALS, True),
        (0.85, "0.5 - 0.9", FilterOp.IN_RANGE, True),
        (0.85, "0.5..0.8", FilterOp.IN_RANGE, False),
        # Stringified value with non-numeric input falls through to text-style
        # comparison rather than hiding everything.
        (0.85, "0.8", FilterOp.CONTAINS, True),
    ],
)
def test_number_ops(value, text, op, expected):
    assert matches(value, text, op, kind=ColumnKind.NUMERIC) is expected


# Blank ops


@pytest.mark.parametrize(
    "value, expected_blank, expected_not_blank",
    [
        ("", True, False),
        (None, True, False),
        ("hello", False, True),
        (0, False, True),
    ],
)
def test_blank_ops(value, expected_blank, expected_not_blank):
    assert matches(value, "", FilterOp.BLANK, kind=ColumnKind.TEXT) is expected_blank
    assert matches(value, "", FilterOp.NOT_BLANK, kind=ColumnKind.TEXT) is expected_not_blank


def test_empty_text_with_value_op_is_inactive():
    """A typing-mode op with empty input must not filter anything out."""
    assert matches("anything", "", FilterOp.CONTAINS, kind=ColumnKind.TEXT) is True
    assert matches(0.5, "", FilterOp.GREATER_THAN, kind=ColumnKind.NUMERIC) is True


def test_numeric_op_with_unparseable_text_is_inactive():
    """While the user is mid-typing, pass everything through."""
    assert matches(0.5, "0.", FilterOp.GREATER_THAN, kind=ColumnKind.NUMERIC) is True
    assert matches(0.5, "abc", FilterOp.EQUALS, kind=ColumnKind.NUMERIC) is True


def test_numeric_op_on_blank_value_is_filtered_out():
    """Numeric ops with a real value cannot match a blank cell."""
    assert matches(None, "0.5", FilterOp.GREATER_THAN, kind=ColumnKind.NUMERIC) is False
    assert matches("", "0.5", FilterOp.EQUALS, kind=ColumnKind.NUMERIC) is False


# Range/date/time/set parsers


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2026-04-25", date(2026, 4, 25)),
        ("2026-04-25 08:00:00", date(2026, 4, 25)),
        ("2026-04-25T08:00:00", date(2026, 4, 25)),
        (" 2026-04-25 ", date(2026, 4, 25)),
        ("2026-04", None),
        ("garbage", None),
        ("", None),
    ],
)
def test_parse_date(text, expected):
    assert parse_date(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2026-04-25 .. 2026-05-02", (date(2026, 4, 25), date(2026, 5, 2))),
        ("2026-04-25..2026-05-02", (date(2026, 4, 25), date(2026, 5, 2))),
        ("2026-04-25 - 2026-05-02", (date(2026, 4, 25), date(2026, 5, 2))),
        # Reversed bounds swap.
        ("2026-05-02 .. 2026-04-25", (date(2026, 4, 25), date(2026, 5, 2))),
        # A single date contains hyphens; it must not split on them.
        ("2026-04-25", None),
        ("2026-04-25 .. garbage", None),
        ("", None),
    ],
)
def test_parse_date_range(text, expected):
    assert parse_date_range(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("04:00", time(4, 0)),
        ("04:00:30", time(4, 0, 30)),
        ("24:00", None),
        ("garbage", None),
    ],
)
def test_parse_time(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("04:00 - 08:00", (time(4, 0), time(8, 0))),
        ("04:00..08:00", (time(4, 0), time(8, 0))),
        # Start after end means an overnight window; bounds must NOT swap.
        ("22:00 - 04:00", (time(22, 0), time(4, 0))),
        ("04:00", None),
        ("04:00 - later", None),
    ],
)
def test_parse_time_range(text, expected):
    assert parse_time_range(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("MSD-109; MSD-110", ["MSD-109", "MSD-110"]),
        ("  a ;b;  ", ["a", "b"]),
        (";;", []),
        ("", []),
    ],
)
def test_parse_set_values(text, expected):
    assert parse_set_values(text) == expected


# Datetime ops (cells are ISO datetime strings)


@pytest.mark.parametrize(
    "value, text, op, expected",
    [
        # ON_DATE matches any time on that day, both cell formats.
        ("2026-04-25 08:00:00", "2026-04-25", FilterOp.ON_DATE, True),
        ("2026-04-25T23:59:59", "2026-04-25", FilterOp.ON_DATE, True),
        ("2026-04-26 00:00:00", "2026-04-25", FilterOp.ON_DATE, False),
        # BEFORE/AFTER are strict on the date part; same-day never matches.
        ("2026-04-25 08:00:00", "2026-04-25", FilterOp.BEFORE_DATE, False),
        ("2026-04-24 23:59:59", "2026-04-25", FilterOp.BEFORE_DATE, True),
        ("2026-04-25 23:59:59", "2026-04-25", FilterOp.AFTER_DATE, False),
        ("2026-04-26 00:00:00", "2026-04-25", FilterOp.AFTER_DATE, True),
        # DATE_RANGE is inclusive at both ends.
        ("2026-04-25 00:00:00", "2026-04-25 .. 2026-05-02", FilterOp.DATE_RANGE, True),
        ("2026-05-02 23:00:00", "2026-04-25 .. 2026-05-02", FilterOp.DATE_RANGE, True),
        ("2026-05-03 00:00:00", "2026-04-25 .. 2026-05-02", FilterOp.DATE_RANGE, False),
        ("2026-04-24 23:59:59", "2026-04-25 .. 2026-05-02", FilterOp.DATE_RANGE, False),
        # TIME_OF_DAY_RANGE is inclusive and spans all dates.
        ("2026-04-25 04:00:00", "04:00 - 08:00", FilterOp.TIME_OF_DAY_RANGE, True),
        ("2026-06-30 08:00:00", "04:00 - 08:00", FilterOp.TIME_OF_DAY_RANGE, True),
        ("2026-04-25 03:59:59", "04:00 - 08:00", FilterOp.TIME_OF_DAY_RANGE, False),
        ("2026-04-25 12:00:00", "04:00 - 08:00", FilterOp.TIME_OF_DAY_RANGE, False),
        # Overnight window wraps midnight.
        ("2026-04-25 23:30:00", "22:00 - 04:00", FilterOp.TIME_OF_DAY_RANGE, True),
        ("2026-04-25 03:00:00", "22:00 - 04:00", FilterOp.TIME_OF_DAY_RANGE, True),
        ("2026-04-25 12:00:00", "22:00 - 04:00", FilterOp.TIME_OF_DAY_RANGE, False),
    ],
)
def test_datetime_ops(value, text, op, expected):
    assert matches(value, text, op, kind=ColumnKind.DATETIME) is expected


def test_datetime_op_with_unparseable_text_is_inactive():
    """While the user is mid-typing a date, pass everything through."""
    assert matches("2026-04-25 08:00:00", "2026-0", FilterOp.ON_DATE, kind=ColumnKind.DATETIME) is True
    assert matches("2026-04-25 08:00:00", "04:0", FilterOp.TIME_OF_DAY_RANGE, kind=ColumnKind.DATETIME) is True


def test_datetime_op_on_unparseable_cell_is_filtered_out():
    """Date ops with a valid filter cannot match a blank or garbage cell."""
    assert matches(None, "2026-04-25", FilterOp.ON_DATE, kind=ColumnKind.DATETIME) is False
    assert matches("", "2026-04-25", FilterOp.ON_DATE, kind=ColumnKind.DATETIME) is False
    assert matches("garbage", "2026-04-25", FilterOp.ON_DATE, kind=ColumnKind.DATETIME) is False


def test_datetime_op_on_non_datetime_kind_is_inert():
    assert matches("hello", "2026-04-25", FilterOp.ON_DATE, kind=ColumnKind.TEXT) is True


def test_datetime_column_still_supports_text_ops():
    assert matches("2026-04-25 08:00:00", "2026-04", FilterOp.CONTAINS, kind=ColumnKind.DATETIME) is True
    assert matches("2026-04-25 08:00:00", "2026-05", FilterOp.CONTAINS, kind=ColumnKind.DATETIME) is False


# IS_ANY_OF


@pytest.mark.parametrize(
    "value, text, expected",
    [
        ("MSD-109", "MSD-109; MSD-110", True),
        ("msd-110", "MSD-109; MSD-110", True),
        ("MSD-111", "MSD-109; MSD-110", False),
        # Membership is exact per value, not substring.
        ("MSD-10", "MSD-109; MSD-110", False),
        (None, "MSD-109", False),
        ("", "MSD-109", False),
        # Only separators typed: filter inactive.
        ("MSD-109", ";;", True),
    ],
)
def test_is_any_of(value, text, expected):
    assert matches(value, text, FilterOp.IS_ANY_OF, kind=ColumnKind.CATEGORICAL) is expected
