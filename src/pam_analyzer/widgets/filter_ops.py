"""Filter operators for the per-column header filter row.

Mirrors the subset of AG Grid's ``agTextColumnFilter`` and
``agNumberColumnFilter`` operators that the floating filter exposes. Each
operator knows how to test a cell value against the user's typed text.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

# polars is imported lazily inside to_polars_expr so it isn't a hard dep for the whole app
if TYPE_CHECKING:
    import polars as pl


class FilterOp(Enum):
    # Text + universal
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    BLANK = "blank"
    NOT_BLANK = "not_blank"
    # Numeric
    GREATER_THAN = "greater_than"
    GREATER_THAN_OR_EQUAL = "greater_than_or_equal"
    LESS_THAN = "less_than"
    LESS_THAN_OR_EQUAL = "less_than_or_equal"
    IN_RANGE = "in_range"


@dataclass(frozen=True)
class _OpMeta:
    op: FilterOp
    label: str
    needs_value: bool = True  # blank/not_blank don't


_TEXT_OP_META: tuple[_OpMeta, ...] = (
    _OpMeta(FilterOp.CONTAINS, "Contains"),
    _OpMeta(FilterOp.NOT_CONTAINS, "Not contains"),
    _OpMeta(FilterOp.EQUALS, "Equals"),
    _OpMeta(FilterOp.NOT_EQUALS, "Not equals"),
    _OpMeta(FilterOp.STARTS_WITH, "Starts with"),
    _OpMeta(FilterOp.ENDS_WITH, "Ends with"),
    _OpMeta(FilterOp.BLANK, "Blank", needs_value=False),
    _OpMeta(FilterOp.NOT_BLANK, "Not blank", needs_value=False),
)

_NUMBER_OP_META: tuple[_OpMeta, ...] = (
    _OpMeta(FilterOp.EQUALS, "Equals"),
    _OpMeta(FilterOp.NOT_EQUALS, "Not equals"),
    _OpMeta(FilterOp.GREATER_THAN, "Greater than"),
    _OpMeta(FilterOp.GREATER_THAN_OR_EQUAL, "Greater than or equal"),
    _OpMeta(FilterOp.LESS_THAN, "Less than"),
    _OpMeta(FilterOp.LESS_THAN_OR_EQUAL, "Less than or equal"),
    _OpMeta(FilterOp.IN_RANGE, "In range (min - max)"),
    _OpMeta(FilterOp.BLANK, "Blank", needs_value=False),
    _OpMeta(FilterOp.NOT_BLANK, "Not blank", needs_value=False),
)


TEXT_OPS: tuple[FilterOp, ...] = tuple(m.op for m in _TEXT_OP_META)
NUMBER_OPS: tuple[FilterOp, ...] = tuple(m.op for m in _NUMBER_OP_META)

DEFAULT_TEXT_OP = FilterOp.CONTAINS
DEFAULT_NUMBER_OP = FilterOp.EQUALS


def operators_for(numeric: bool) -> tuple[FilterOp, ...]:
    return NUMBER_OPS if numeric else TEXT_OPS


def default_op(numeric: bool) -> FilterOp:
    return DEFAULT_NUMBER_OP if numeric else DEFAULT_TEXT_OP


def label_for(op: FilterOp) -> str:
    for meta in (*_NUMBER_OP_META, *_TEXT_OP_META):
        if meta.op is op:
            return meta.label
    return op.value


def needs_value(op: FilterOp) -> bool:
    return op not in (FilterOp.BLANK, FilterOp.NOT_BLANK)


def _parse_range(text: str) -> tuple[float, float] | None:
    """Parse "min - max" or "min..max" into (min, max). Returns None on failure."""
    for sep in (" - ", "..", "-"):
        if sep in text:
            left, _, right = text.partition(sep)
            left, right = left.strip(), right.strip()
            if not left or not right:
                continue
            try:
                lo, hi = float(left), float(right)
            except ValueError:
                continue
            if lo > hi:
                lo, hi = hi, lo
            return lo, hi
    return None


def matches(value: object, text: str, op: FilterOp, numeric: bool) -> bool:
    """Return whether *value* passes the *op*/*text* filter.

    ``text`` is the raw user input from the floating filter. For ops that
    don't take a value (BLANK and NOT_BLANK) it is ignored.
    """
    is_blank = value is None or value == ""

    if op is FilterOp.BLANK:
        return is_blank
    if op is FilterOp.NOT_BLANK:
        return not is_blank

    text = text.strip()
    if not text:
        # No value typed, so let everything through (filter is inactive).
        return True

    if numeric:
        if is_blank:
            return False
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False
        if op is FilterOp.IN_RANGE:
            rng = _parse_range(text)
            if rng is None:
                # Malformed range. Treat as inactive rather than hide everything.
                return True
            lo, hi = rng
            return lo <= v <= hi
        try:
            target = float(text)
        except ValueError:
            # Not parseable as number yet (user mid-typing), so do not filter.
            return True
        if op is FilterOp.EQUALS:
            return v == target
        if op is FilterOp.NOT_EQUALS:
            return v != target
        if op is FilterOp.GREATER_THAN:
            return v > target
        if op is FilterOp.GREATER_THAN_OR_EQUAL:
            return v >= target
        if op is FilterOp.LESS_THAN:
            return v < target
        if op is FilterOp.LESS_THAN_OR_EQUAL:
            return v <= target
        # Fall through: text-style ops on a numeric column compare stringified.
        haystack = str(value).casefold()
        needle = text.casefold()
        return _text_match(haystack, needle, op)

    # Text column
    haystack = ("" if value is None else str(value)).casefold()
    needle = text.casefold()
    return _text_match(haystack, needle, op)


def _text_match(haystack: str, needle: str, op: FilterOp) -> bool:
    if op is FilterOp.CONTAINS:
        return needle in haystack
    if op is FilterOp.NOT_CONTAINS:
        return needle not in haystack
    if op is FilterOp.EQUALS:
        return haystack == needle
    if op is FilterOp.NOT_EQUALS:
        return haystack != needle
    if op is FilterOp.STARTS_WITH:
        return haystack.startswith(needle)
    if op is FilterOp.ENDS_WITH:
        return haystack.endswith(needle)
    return True


def to_polars_expr(col: str, text: str, op: FilterOp, numeric: bool) -> pl.Expr:
    """Return a Polars boolean expression equivalent to ``matches()`` for *col*.

    Import polars lazily so the rest of the app doesn't require it.
    """
    import polars as pl

    raw = pl.col(col)
    as_str = raw.cast(pl.String)

    if op is FilterOp.BLANK:
        return raw.is_null() | (as_str == "")
    if op is FilterOp.NOT_BLANK:
        return raw.is_not_null() & (as_str != "")

    text = text.strip()

    if numeric:
        num = raw.cast(pl.Float64, strict=False)
        if op is FilterOp.IN_RANGE:
            rng = _parse_range(text)
            if rng is None:
                return pl.lit(True)
            lo, hi = rng
            return (num >= lo) & (num <= hi)
        try:
            target = float(text)
        except ValueError:
            return pl.lit(True)
        if op is FilterOp.EQUALS:                return num == target           # noqa: E701
        if op is FilterOp.NOT_EQUALS:            return num != target           # noqa: E701
        if op is FilterOp.GREATER_THAN:          return num > target            # noqa: E701
        if op is FilterOp.GREATER_THAN_OR_EQUAL: return num >= target           # noqa: E701
        if op is FilterOp.LESS_THAN:             return num < target            # noqa: E701
        if op is FilterOp.LESS_THAN_OR_EQUAL:    return num <= target           # noqa: E701
        return pl.lit(True)

    # Text ops, case-insensitive (mirrors matches() casefold behaviour)
    needle = text.casefold()
    lowered = as_str.str.to_lowercase()
    if op is FilterOp.CONTAINS:     return lowered.str.contains(needle, literal=True)  # noqa: E701
    if op is FilterOp.NOT_CONTAINS: return ~lowered.str.contains(needle, literal=True) # noqa: E701
    if op is FilterOp.EQUALS:       return lowered == needle                           # noqa: E701
    if op is FilterOp.NOT_EQUALS:   return lowered != needle                           # noqa: E701
    if op is FilterOp.STARTS_WITH:  return lowered.str.starts_with(needle)             # noqa: E701
    if op is FilterOp.ENDS_WITH:    return lowered.str.ends_with(needle)               # noqa: E701
    return pl.lit(True)
