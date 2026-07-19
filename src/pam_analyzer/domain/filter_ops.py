"""Filter operators for the per-column header filter row.

Mirrors the subset of AG Grid's ``agTextColumnFilter`` and
``agNumberColumnFilter`` operators that the floating filter exposes. Each
operator knows how to test a cell value against the user's typed text.

The polars translation of these semantics lives in
``pam_analyzer.ui.models.filter_exprs``. This layer is stdlib-only, and the
two implementations are pinned together by a parity test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum


class ColumnKind(Enum):
    """Filtering behavior of a column, driving its operator menu.

    DATETIME columns hold ISO datetime strings (Recording_Time).
    CATEGORICAL columns hold low-cardinality text and additionally offer
    the "is one of" checkbox popup.
    """

    TEXT = "text"
    NUMERIC = "numeric"
    DATETIME = "datetime"
    CATEGORICAL = "categorical"


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
    # Categorical
    IS_ANY_OF = "is_any_of"
    # Datetime (cells are ISO datetime strings)
    ON_DATE = "on_date"
    BEFORE_DATE = "before_date"
    AFTER_DATE = "after_date"
    DATE_RANGE = "date_range"
    TIME_OF_DAY_RANGE = "time_of_day_range"


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


# Ellipsis in a label signals that picking the op opens an editor popup.
_DATETIME_OP_META: tuple[_OpMeta, ...] = (
    _OpMeta(FilterOp.ON_DATE, "On date..."),
    _OpMeta(FilterOp.BEFORE_DATE, "Before date..."),
    _OpMeta(FilterOp.AFTER_DATE, "After date..."),
    _OpMeta(FilterOp.DATE_RANGE, "Date range..."),
    _OpMeta(FilterOp.TIME_OF_DAY_RANGE, "Time of day..."),
    *_TEXT_OP_META,
)

_CATEGORICAL_OP_META: tuple[_OpMeta, ...] = (
    _OpMeta(FilterOp.IS_ANY_OF, "Is one of..."),
    *_TEXT_OP_META,
)

_META_BY_KIND: dict[ColumnKind, tuple[_OpMeta, ...]] = {
    ColumnKind.TEXT: _TEXT_OP_META,
    ColumnKind.NUMERIC: _NUMBER_OP_META,
    ColumnKind.DATETIME: _DATETIME_OP_META,
    ColumnKind.CATEGORICAL: _CATEGORICAL_OP_META,
}

TEXT_OPS: tuple[FilterOp, ...] = tuple(m.op for m in _TEXT_OP_META)
NUMBER_OPS: tuple[FilterOp, ...] = tuple(m.op for m in _NUMBER_OP_META)

DEFAULT_TEXT_OP = FilterOp.CONTAINS
DEFAULT_NUMBER_OP = FilterOp.EQUALS

# Ops that dispatch on the parsed datetime cell rather than its raw text.
DATETIME_OPS: frozenset[FilterOp] = frozenset(
    {
        FilterOp.ON_DATE,
        FilterOp.BEFORE_DATE,
        FilterOp.AFTER_DATE,
        FilterOp.DATE_RANGE,
        FilterOp.TIME_OF_DAY_RANGE,
    }
)

# Ops that compare numerically on NUMERIC columns. EQUALS and NOT_EQUALS
# appear in both the text and number menus; the column kind decides which
# comparison applies. Text-style ops on a numeric column compare stringified.
NUMERIC_COMPARE_OPS: frozenset[FilterOp] = frozenset(
    {
        FilterOp.EQUALS,
        FilterOp.NOT_EQUALS,
        FilterOp.GREATER_THAN,
        FilterOp.GREATER_THAN_OR_EQUAL,
        FilterOp.LESS_THAN,
        FilterOp.LESS_THAN_OR_EQUAL,
        FilterOp.IN_RANGE,
    }
)


def operators_for(kind: ColumnKind) -> tuple[FilterOp, ...]:
    return tuple(m.op for m in _META_BY_KIND[kind])


def default_op(kind: ColumnKind) -> FilterOp:
    # DATETIME and CATEGORICAL default to CONTAINS so plain typing behaves
    # exactly as before; the rich ops are opt-in via the funnel menu.
    return DEFAULT_NUMBER_OP if kind is ColumnKind.NUMERIC else DEFAULT_TEXT_OP


def label_for(op: FilterOp) -> str:
    for meta in (*_NUMBER_OP_META, *_TEXT_OP_META, *_DATETIME_OP_META, *_CATEGORICAL_OP_META):
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


def _split_range(text: str) -> tuple[str, str] | None:
    """Split a range on ".." or a spaced " - ".

    Unlike _parse_range, a bare "-" is never a separator here: ISO dates
    contain hyphens, so "2026-04-25" must not split.
    """
    for sep in ("..", " - "):
        if sep in text:
            left, _, right = text.partition(sep)
            left, right = left.strip(), right.strip()
            if left and right:
                return left, right
    return None


def parse_date(text: str) -> date | None:
    """Parse an ISO date; a full datetime is accepted, its time part ignored."""
    try:
        return datetime.fromisoformat(text.strip()).date()
    except ValueError:
        return None


def parse_date_range(text: str) -> tuple[date, date] | None:
    """Parse "YYYY-MM-DD .. YYYY-MM-DD" (or spaced " - "). Swaps if reversed."""
    parts = _split_range(text)
    if parts is None:
        return None
    lo, hi = parse_date(parts[0]), parse_date(parts[1])
    if lo is None or hi is None:
        return None
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def parse_time(text: str) -> time | None:
    """Parse "HH:MM" or "HH:MM:SS"."""
    try:
        return time.fromisoformat(text.strip())
    except ValueError:
        return None


def parse_time_range(text: str) -> tuple[time, time] | None:
    """Parse "HH:MM - HH:MM" (or ".."). Never swaps: start after end
    means an overnight window that wraps midnight."""
    parts = _split_range(text)
    if parts is None:
        return None
    lo, hi = parse_time(parts[0]), parse_time(parts[1])
    if lo is None or hi is None:
        return None
    return lo, hi


def parse_set_values(text: str) -> list[str]:
    """Split "a; b; c" into its values, dropping blanks."""
    return [part for part in (p.strip() for p in text.split(";")) if part]


def _matches_datetime(value: object, text: str, op: FilterOp) -> bool:
    """Date/time ops against an ISO datetime cell.

    Filter text is validated before the cell so a malformed filter is
    inactive (keeps everything, unparseable cells included), matching the
    polars translation's pl.lit(True). A valid filter then excludes
    unparseable cells, like null helper columns failing every comparison.
    """
    target: tuple[time, time] | tuple[date, date] | date | None
    if op is FilterOp.TIME_OF_DAY_RANGE:
        target = parse_time_range(text)
    elif op is FilterOp.DATE_RANGE:
        target = parse_date_range(text)
    else:
        target = parse_date(text)
    if target is None:
        return True
    try:
        cell = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return False
    if op is FilterOp.TIME_OF_DAY_RANGE:
        lo, hi = target
        t = cell.time()
        if lo <= hi:
            return lo <= t <= hi
        return t >= lo or t <= hi
    if op is FilterOp.DATE_RANGE:
        dlo, dhi = target
        return dlo <= cell.date() <= dhi
    d = cell.date()
    if op is FilterOp.ON_DATE:
        return d == target
    if op is FilterOp.BEFORE_DATE:
        return d < target
    return d > target  # AFTER_DATE


def matches(value: object, text: str, op: FilterOp, kind: ColumnKind) -> bool:
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

    if op is FilterOp.IS_ANY_OF:
        values = parse_set_values(text)
        if not values:
            return True
        cell = ("" if value is None else str(value)).casefold()
        return cell in {v.casefold() for v in values}

    if op in DATETIME_OPS:
        # Gated on kind so a stray date op on a non-datetime column is
        # inert instead of matching raw text against a parsed date.
        if kind is not ColumnKind.DATETIME:
            return True
        return _matches_datetime(value, text, op)

    if kind is ColumnKind.NUMERIC and op in NUMERIC_COMPARE_OPS:
        # Filter text is validated before the cell so a malformed filter is
        # inactive (keeps everything, blank cells included), matching the
        # polars translation's pl.lit(True).
        if op is FilterOp.IN_RANGE:
            rng = _parse_range(text)
            if rng is None:
                return True
            if is_blank:
                return False
            try:
                v = float(value)
            except (TypeError, ValueError):
                return False
            lo, hi = rng
            return lo <= v <= hi
        try:
            target = float(text)
        except ValueError:
            # Not parseable as number yet (user mid-typing), so do not filter.
            return True
        if is_blank:
            return False
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False
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
        return v <= target  # LESS_THAN_OR_EQUAL

    # Text-style ops (any kind; numeric/datetime cells compare stringified).
    # None fails every value-taking string op, mirroring polars where a null
    # cell nulls out the comparison and the row is dropped.
    if value is None:
        return False
    haystack = str(value).casefold()
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
