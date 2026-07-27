"""Filter operators for the per-column header filter row.

Each operator is a FilterOperator object that owns everything about itself: its
identity (FilterOp), its menu label, whether it takes a typed value, and BOTH
implementations of its matching rule, the scalar one used by unit tests and the
polars one used by the live table model. The two live side by side in one class
so they cannot drift apart unnoticed. A parity test in
tests/domain/test_filter_polars_parity.py compares them for every registered operator.

The operators mirror the subset of a spreadsheet-style column filter that the
floating filter row exposes. The polars frame that the table model filters carries per-column datetime
helper columns (built by datetime_helper_exprs below). Datetime operators read
those by name via date_helper_col / time_helper_col.

DATETIME cells are ISO datetime strings. A DATETIME operator is inert (keeps
every row) on a non-DATETIME column, so a stray date op never matches raw text
against a parsed date.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, time
from enum import Enum

import polars as pl


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


# Datetime operators read the pre-parsed helper columns the table model adds
# once when it builds the sort frame, so per-keystroke filtering never re-parses
# the strings. The names are defined here because the operators reference them.


def date_helper_col(col: str) -> str:
    """Name of the hidden date-part helper column for *col* in the sort frame."""
    return f"__{col}__date"


def time_helper_col(col: str) -> str:
    """Name of the hidden time-of-day helper column for *col* in the sort frame."""
    return f"__{col}__time"


def datetime_helper_exprs(col: str) -> list[pl.Expr]:
    """Expressions adding parsed date/time helper columns for a DATETIME column.

    Parsed once when the table model builds its sort frame so per-keystroke
    filtering never re-parses the strings. Formats are coalesced explicitly
    because to_datetime() inference locks onto the first non-null cell's format,
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
    """Parse an ISO date. A full datetime is accepted, its time part ignored."""
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


def _is_blank(value: object) -> bool:
    return value is None or value == ""


class FilterOperator(ABC):
    """One filter operator: identity, menu metadata, and both matching forms.

    The base owns the rule shared by every value-taking operator: an empty
    filter box means the filter is inactive and keeps every row. BLANK and
    NOT_BLANK set needs_value = False and skip that shortcut. Subclasses supply
    only the operator-specific comparison in _matches / _to_polars.
    """

    op: FilterOp
    label: str
    needs_value: bool = True

    def matches(self, value: object, text: str, kind: ColumnKind) -> bool:
        """Scalar form: whether one cell value passes this filter."""
        if self.needs_value:
            text = text.strip()
            if not text:
                return True
        return self._matches(value, text, kind)

    def to_polars(self, col: str, text: str, kind: ColumnKind) -> pl.Expr:
        """Vectorized form: a boolean expression equivalent to matches()."""
        if self.needs_value:
            text = text.strip()
            if not text:
                return pl.lit(True)
        return self._to_polars(col, text, kind)

    @abstractmethod
    def _matches(self, value: object, text: str, kind: ColumnKind) -> bool: ...

    @abstractmethod
    def _to_polars(self, col: str, text: str, kind: ColumnKind) -> pl.Expr: ...


class _TextOperator(FilterOperator):
    """Text-style op: compares the casefolded stringified cell. Applies to any
    column kind. Numeric and datetime cells compare as their stringified form.
    A null cell fails every value-taking text op (mirrors polars dropping the
    null-valued comparison)."""

    def _matches(self, value: object, text: str, kind: ColumnKind) -> bool:
        if value is None:
            return False
        return self._compare(str(value).casefold(), text.casefold())

    def _to_polars(self, col: str, text: str, kind: ColumnKind) -> pl.Expr:
        lowered = pl.col(col).cast(pl.String).str.to_lowercase()
        return self._pl(lowered, text.casefold())

    @abstractmethod
    def _compare(self, haystack: str, needle: str) -> bool: ...

    @abstractmethod
    def _pl(self, lowered: pl.Expr, needle: str) -> pl.Expr: ...


class Contains(_TextOperator):
    op, label = FilterOp.CONTAINS, "Contains"

    def _compare(self, haystack, needle):
        return needle in haystack

    def _pl(self, lowered, needle):
        return lowered.str.contains(needle, literal=True)


class NotContains(_TextOperator):
    op, label = FilterOp.NOT_CONTAINS, "Not contains"

    def _compare(self, haystack, needle):
        return needle not in haystack

    def _pl(self, lowered, needle):
        return ~lowered.str.contains(needle, literal=True)


class StartsWith(_TextOperator):
    op, label = FilterOp.STARTS_WITH, "Starts with"

    def _compare(self, haystack, needle):
        return haystack.startswith(needle)

    def _pl(self, lowered, needle):
        return lowered.str.starts_with(needle)


class EndsWith(_TextOperator):
    op, label = FilterOp.ENDS_WITH, "Ends with"

    def _compare(self, haystack, needle):
        return haystack.endswith(needle)

    def _pl(self, lowered, needle):
        return lowered.str.ends_with(needle)


class Equals(FilterOperator):
    """Dual by design: numeric equality on a NUMERIC column, casefolded string
    equality on any other. The kind-dependence is real domain meaning and now
    lives in one place instead of being split across two switch statements."""

    op, label = FilterOp.EQUALS, "Equals"

    def _matches(self, value, text, kind):
        if kind is ColumnKind.NUMERIC:
            try:
                target = float(text)
            except ValueError:
                return True
            if _is_blank(value):
                return False
            try:
                return float(value) == target
            except (TypeError, ValueError):
                return False
        if value is None:
            return False
        return str(value).casefold() == text.casefold()

    def _to_polars(self, col, text, kind):
        if kind is ColumnKind.NUMERIC:
            try:
                target = float(text)
            except ValueError:
                return pl.lit(True)
            return pl.col(col).cast(pl.Float64, strict=False) == target
        return pl.col(col).cast(pl.String).str.to_lowercase() == text.casefold()


class NotEquals(FilterOperator):
    op, label = FilterOp.NOT_EQUALS, "Not equals"

    def _matches(self, value, text, kind):
        if kind is ColumnKind.NUMERIC:
            try:
                target = float(text)
            except ValueError:
                return True
            if _is_blank(value):
                return False
            try:
                return float(value) != target
            except (TypeError, ValueError):
                return False
        if value is None:
            return False
        return str(value).casefold() != text.casefold()

    def _to_polars(self, col, text, kind):
        if kind is ColumnKind.NUMERIC:
            try:
                target = float(text)
            except ValueError:
                return pl.lit(True)
            return pl.col(col).cast(pl.Float64, strict=False) != target
        return pl.col(col).cast(pl.String).str.to_lowercase() != text.casefold()


class _NumericOperator(FilterOperator):
    """Numeric comparison against a single typed number. Only offered on NUMERIC
    columns. On any other kind it degrades to the inert text-style behavior of
    an unrecognised op (keeps non-null cells) so a misapplied op never hides
    rows. Unparseable filter text is inactive (user mid-typing). A blank cell
    never matches a real filter."""

    def _matches(self, value, text, kind):
        if kind is not ColumnKind.NUMERIC:
            return value is not None
        try:
            target = float(text)
        except ValueError:
            return True
        if _is_blank(value):
            return False
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False
        return self._num(v, target)

    def _to_polars(self, col, text, kind):
        if kind is not ColumnKind.NUMERIC:
            return pl.lit(True)
        try:
            target = float(text)
        except ValueError:
            return pl.lit(True)
        return self._num_expr(pl.col(col).cast(pl.Float64, strict=False), target)

    @abstractmethod
    def _num(self, v: float, target: float) -> bool: ...

    @abstractmethod
    def _num_expr(self, num: pl.Expr, target: float) -> pl.Expr: ...


class GreaterThan(_NumericOperator):
    op, label = FilterOp.GREATER_THAN, "Greater than"

    def _num(self, v, target):
        return v > target

    def _num_expr(self, num, target):
        return num > target


class GreaterThanOrEqual(_NumericOperator):
    op, label = FilterOp.GREATER_THAN_OR_EQUAL, "Greater than or equal"

    def _num(self, v, target):
        return v >= target

    def _num_expr(self, num, target):
        return num >= target


class LessThan(_NumericOperator):
    op, label = FilterOp.LESS_THAN, "Less than"

    def _num(self, v, target):
        return v < target

    def _num_expr(self, num, target):
        return num < target


class LessThanOrEqual(_NumericOperator):
    op, label = FilterOp.LESS_THAN_OR_EQUAL, "Less than or equal"

    def _num(self, v, target):
        return v <= target

    def _num_expr(self, num, target):
        return num <= target


class InRange(FilterOperator):
    op, label = FilterOp.IN_RANGE, "In range (min - max)"

    def _matches(self, value, text, kind):
        if kind is not ColumnKind.NUMERIC:
            return value is not None
        rng = _parse_range(text)
        if rng is None:
            return True
        if _is_blank(value):
            return False
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False
        lo, hi = rng
        return lo <= v <= hi

    def _to_polars(self, col, text, kind):
        if kind is not ColumnKind.NUMERIC:
            return pl.lit(True)
        rng = _parse_range(text)
        if rng is None:
            return pl.lit(True)
        lo, hi = rng
        num = pl.col(col).cast(pl.Float64, strict=False)
        return (num >= lo) & (num <= hi)


class Blank(FilterOperator):
    op, label, needs_value = FilterOp.BLANK, "Blank", False

    def _matches(self, value, text, kind):
        return _is_blank(value)

    def _to_polars(self, col, text, kind):
        raw = pl.col(col)
        return raw.is_null() | (raw.cast(pl.String) == "")


class NotBlank(FilterOperator):
    op, label, needs_value = FilterOp.NOT_BLANK, "Not blank", False

    def _matches(self, value, text, kind):
        return not _is_blank(value)

    def _to_polars(self, col, text, kind):
        raw = pl.col(col)
        return raw.is_not_null() & (raw.cast(pl.String) != "")


class IsAnyOf(FilterOperator):
    op, label = FilterOp.IS_ANY_OF, "Is one of..."

    def _matches(self, value, text, kind):
        values = parse_set_values(text)
        if not values:
            return True
        cell = ("" if value is None else str(value)).casefold()
        return cell in {v.casefold() for v in values}

    def _to_polars(self, col, text, kind):
        values = parse_set_values(text)
        if not values:
            return pl.lit(True)
        return pl.col(col).cast(pl.String).str.to_lowercase().is_in([v.casefold() for v in values])


class _DateTimeOperator(FilterOperator):
    """Date/time op against an ISO datetime cell. Inert on non-DATETIME columns.
    The scalar form parses each cell string. The polars form reads the pre-parsed
    helper column. An unparseable filter is inactive (keeps everything). A valid
    filter then excludes unparseable cells (they fail every comparison)."""

    def _matches(self, value, text, kind):
        if kind is not ColumnKind.DATETIME:
            return True
        target = self._parse_target(text)
        if target is None:
            return True
        try:
            cell = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return False
        return self._cmp(cell, target)

    def _to_polars(self, col, text, kind):
        if kind is not ColumnKind.DATETIME:
            return pl.lit(True)
        target = self._parse_target(text)
        if target is None:
            return pl.lit(True)
        return self._expr(col, target)

    @abstractmethod
    def _parse_target(self, text: str): ...

    @abstractmethod
    def _cmp(self, cell: datetime, target) -> bool: ...

    @abstractmethod
    def _expr(self, col: str, target) -> pl.Expr: ...


class OnDate(_DateTimeOperator):
    op, label = FilterOp.ON_DATE, "On date..."

    def _parse_target(self, text):
        return parse_date(text)

    def _cmp(self, cell, target):
        return cell.date() == target

    def _expr(self, col, target):
        return pl.col(date_helper_col(col)) == target


class BeforeDate(_DateTimeOperator):
    op, label = FilterOp.BEFORE_DATE, "Before date..."

    def _parse_target(self, text):
        return parse_date(text)

    def _cmp(self, cell, target):
        return cell.date() < target

    def _expr(self, col, target):
        return pl.col(date_helper_col(col)) < target


class AfterDate(_DateTimeOperator):
    op, label = FilterOp.AFTER_DATE, "After date..."

    def _parse_target(self, text):
        return parse_date(text)

    def _cmp(self, cell, target):
        return cell.date() > target

    def _expr(self, col, target):
        return pl.col(date_helper_col(col)) > target


class DateRange(_DateTimeOperator):
    op, label = FilterOp.DATE_RANGE, "Date range..."

    def _parse_target(self, text):
        return parse_date_range(text)

    def _cmp(self, cell, target):
        lo, hi = target
        return lo <= cell.date() <= hi

    def _expr(self, col, target):
        lo, hi = target
        d = pl.col(date_helper_col(col))
        return (d >= lo) & (d <= hi)


class TimeOfDayRange(_DateTimeOperator):
    op, label = FilterOp.TIME_OF_DAY_RANGE, "Time of day..."

    def _parse_target(self, text):
        return parse_time_range(text)

    def _cmp(self, cell, target):
        lo, hi = target
        t = cell.time()
        if lo <= hi:
            return lo <= t <= hi
        return t >= lo or t <= hi

    def _expr(self, col, target):
        lo, hi = target
        t = pl.col(time_helper_col(col))
        if lo <= hi:
            return (t >= lo) & (t <= hi)
        # Overnight window wrapping midnight. Guard against null helpers, which
        # would otherwise pass an OR of two null comparisons.
        return t.is_not_null() & ((t >= lo) | (t <= hi))


_ALL_OPERATORS: tuple[FilterOperator, ...] = (
    Contains(),
    NotContains(),
    Equals(),
    NotEquals(),
    StartsWith(),
    EndsWith(),
    Blank(),
    NotBlank(),
    GreaterThan(),
    GreaterThanOrEqual(),
    LessThan(),
    LessThanOrEqual(),
    InRange(),
    IsAnyOf(),
    OnDate(),
    BeforeDate(),
    AfterDate(),
    DateRange(),
    TimeOfDayRange(),
)

OPERATORS: dict[FilterOp, FilterOperator] = {o.op: o for o in _ALL_OPERATORS}

# Menu order per column kind. This is the one thing that stays a table, because
# it is presentation (which operators a column offers, and in what order), not
# behavior. Behavior lives in the operator classes above.
_TEXT_MENU: tuple[FilterOp, ...] = (
    FilterOp.CONTAINS,
    FilterOp.NOT_CONTAINS,
    FilterOp.EQUALS,
    FilterOp.NOT_EQUALS,
    FilterOp.STARTS_WITH,
    FilterOp.ENDS_WITH,
    FilterOp.BLANK,
    FilterOp.NOT_BLANK,
)
_NUMBER_MENU: tuple[FilterOp, ...] = (
    FilterOp.EQUALS,
    FilterOp.NOT_EQUALS,
    FilterOp.GREATER_THAN,
    FilterOp.GREATER_THAN_OR_EQUAL,
    FilterOp.LESS_THAN,
    FilterOp.LESS_THAN_OR_EQUAL,
    FilterOp.IN_RANGE,
    FilterOp.BLANK,
    FilterOp.NOT_BLANK,
)
# Ellipsis in a datetime/categorical label signals that picking the op opens an
# editor popup.
_DATE_MENU: tuple[FilterOp, ...] = (
    FilterOp.ON_DATE,
    FilterOp.BEFORE_DATE,
    FilterOp.AFTER_DATE,
    FilterOp.DATE_RANGE,
    FilterOp.TIME_OF_DAY_RANGE,
)
_MENU_BY_KIND: dict[ColumnKind, tuple[FilterOp, ...]] = {
    ColumnKind.TEXT: _TEXT_MENU,
    ColumnKind.NUMERIC: _NUMBER_MENU,
    ColumnKind.DATETIME: (*_DATE_MENU, *_TEXT_MENU),
    ColumnKind.CATEGORICAL: (FilterOp.IS_ANY_OF, *_TEXT_MENU),
}

# Ops that dispatch on the parsed datetime cell rather than its raw text. Kept
# for callers that route these ops to an editor popup.
DATETIME_OPS: frozenset[FilterOp] = frozenset(_DATE_MENU)

# Ops that compare numerically on NUMERIC columns.
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
    return _MENU_BY_KIND[kind]


def default_op(kind: ColumnKind) -> FilterOp:
    # DATETIME and CATEGORICAL default to CONTAINS so plain typing behaves as
    # before. The rich ops are opt-in via the funnel menu.
    return FilterOp.EQUALS if kind is ColumnKind.NUMERIC else FilterOp.CONTAINS


def label_for(op: FilterOp) -> str:
    return OPERATORS[op].label


def needs_value(op: FilterOp) -> bool:
    return OPERATORS[op].needs_value


def matches(value: object, text: str, op: FilterOp, kind: ColumnKind) -> bool:
    """Return whether *value* passes the *op*/*text* filter on a *kind* column.

    Thin entry point over the operator registry. The behavior lives on each
    FilterOperator. ``text`` is the raw user input from the floating filter.
    """
    return OPERATORS[op].matches(value, text, kind)
