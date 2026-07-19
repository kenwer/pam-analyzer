"""Polars translation of the filter-op semantics in domain.filter_ops.

Lives next to its only consumer (DetectionsTableModel) rather than in the
domain layer, because the domain is stdlib-only. The pure-Python reference
implementation is ``pam_analyzer.domain.filter_ops.matches``; a parity test
keeps the two in agreement.
"""

from __future__ import annotations

import polars as pl

from ...domain.filter_ops import (
    DATETIME_OPS,
    NUMERIC_COMPARE_OPS,
    ColumnKind,
    FilterOp,
    _parse_range,
    parse_date,
    parse_date_range,
    parse_set_values,
    parse_time_range,
)


def date_helper_col(col: str) -> str:
    """Name of the hidden date-part helper column for *col* in the sort frame."""
    return f"__{col}__date"


def time_helper_col(col: str) -> str:
    """Name of the hidden time-of-day helper column for *col* in the sort frame."""
    return f"__{col}__time"


def datetime_helper_exprs(col: str) -> list[pl.Expr]:
    """Expressions adding parsed date/time helper columns for a DATETIME column.

    Parsed once when the sort frame is built so per-keystroke filtering never
    re-parses the strings. Formats are coalesced explicitly because
    to_datetime() inference locks onto the first non-null cell's format,
    and cells mix "T"/space separators and with/without-seconds forms.
    """
    normalized = pl.col(col).cast(pl.String).str.replace("T", " ", literal=True)
    parsed = pl.coalesce(
        normalized.str.to_datetime("%Y-%m-%d %H:%M:%S", strict=False),
        normalized.str.to_datetime("%Y-%m-%d %H:%M", strict=False),
    )
    return [
        parsed.dt.date().alias(date_helper_col(col)),
        parsed.dt.time().alias(time_helper_col(col)),
    ]


def _datetime_expr(col: str, text: str, op: FilterOp) -> pl.Expr:
    """Date/time ops against the helper columns. Null helpers (unparseable
    cells) fail every comparison, mirroring matches() returning False."""
    if op is FilterOp.TIME_OF_DAY_RANGE:
        trng = parse_time_range(text)
        if trng is None:
            return pl.lit(True)
        tlo, thi = trng
        t = pl.col(time_helper_col(col))
        if tlo <= thi:
            return (t >= tlo) & (t <= thi)
        # Overnight window wrapping midnight. Guard against null helpers,
        # which would otherwise pass an OR of two null comparisons.
        return t.is_not_null() & ((t >= tlo) | (t <= thi))
    d = pl.col(date_helper_col(col))
    if op is FilterOp.DATE_RANGE:
        drng = parse_date_range(text)
        if drng is None:
            return pl.lit(True)
        dlo, dhi = drng
        return (d >= dlo) & (d <= dhi)
    target = parse_date(text)
    if target is None:
        return pl.lit(True)
    if op is FilterOp.ON_DATE:
        return d == target
    if op is FilterOp.BEFORE_DATE:
        return d < target
    return d > target  # AFTER_DATE


def to_polars_expr(col: str, text: str, op: FilterOp, kind: ColumnKind) -> pl.Expr:
    """Return a Polars boolean expression equivalent to ``matches()`` for *col*."""
    raw = pl.col(col)
    as_str = raw.cast(pl.String)

    if op is FilterOp.BLANK:
        return raw.is_null() | (as_str == "")
    if op is FilterOp.NOT_BLANK:
        return raw.is_not_null() & (as_str != "")

    text = text.strip()

    if op is FilterOp.IS_ANY_OF:
        values = parse_set_values(text)
        if not values:
            return pl.lit(True)
        return as_str.str.to_lowercase().is_in([v.casefold() for v in values])

    if op in DATETIME_OPS:
        if kind is not ColumnKind.DATETIME:
            return pl.lit(True)
        return _datetime_expr(col, text, op)

    if kind is ColumnKind.NUMERIC and op in NUMERIC_COMPARE_OPS:
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
        return num <= target  # LESS_THAN_OR_EQUAL

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
