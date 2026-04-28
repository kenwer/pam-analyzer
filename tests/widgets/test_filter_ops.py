"""Unit tests for the operator matcher used by the header filter row."""

import pytest

from pam_analyzer.widgets.filter_ops import FilterOp, matches

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
    assert matches(value, text, op, numeric=False) is expected


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
    assert matches(value, text, op, numeric=True) is expected


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
    assert matches(value, "", FilterOp.BLANK, numeric=False) is expected_blank
    assert matches(value, "", FilterOp.NOT_BLANK, numeric=False) is expected_not_blank


def test_empty_text_with_value_op_is_inactive():
    """A typing-mode op with empty input must not filter anything out."""
    assert matches("anything", "", FilterOp.CONTAINS, numeric=False) is True
    assert matches(0.5, "", FilterOp.GREATER_THAN, numeric=True) is True


def test_numeric_op_with_unparseable_text_is_inactive():
    """While the user is mid-typing, pass everything through."""
    assert matches(0.5, "0.", FilterOp.GREATER_THAN, numeric=True) is True
    assert matches(0.5, "abc", FilterOp.EQUALS, numeric=True) is True


def test_numeric_op_on_blank_value_is_filtered_out():
    """Numeric ops with a real value cannot match a blank cell."""
    assert matches(None, "0.5", FilterOp.GREATER_THAN, numeric=True) is False
    assert matches("", "0.5", FilterOp.EQUALS, numeric=True) is False
