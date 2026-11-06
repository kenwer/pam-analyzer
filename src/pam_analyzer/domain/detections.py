"""Pure functions over detection rows."""

from enum import Enum

import polars as pl

from .filter_ops import parse_datetime_expr


class SpreadKey(Enum):
    """Optional grouping keys for the per-species confidence rank.

    WEEK is the Week column as written, i.e. the BirdNET week (1-48) of the
    week_NN folder the file was imported into, so it agrees with a Week filter
    and with the species list the file was analyzed against. DAY reads the
    detection's own date (see detected_day_expr), not the file start, so a long
    overnight file spreads over both days. MODEL keeps confidences of
    different models apart, as they are not on a comparable scale.
    """

    ARU = "ARU"
    MODEL = "Model"
    WEEK = "Week"
    DAY = "Day"


_DETECTED_DAY_COL = "__detected_day"


def detected_day_expr() -> pl.Expr:
    """Helper column that DAY reads: the date of Recording_Time plus Start_Time.

    Null where Recording_Time is unparsable. Callers add it once per dataset so
    ranking never re-parses the time strings.
    """
    offset = pl.duration(milliseconds=(pl.col("Start_Time").fill_null(0.0) * 1000).round())
    return (parse_datetime_expr("Recording_Time") + offset).dt.date().alias(_DETECTED_DAY_COL)


def _group_expr(key: SpreadKey) -> pl.Expr:
    return pl.col(_DETECTED_DAY_COL if key is SpreadKey.DAY else key.value)


def group_rank_expr(spread: frozenset[SpreadKey]) -> pl.Expr:
    """1-based Confidence rank within Species plus the *spread* keys, highest first.

    DAY needs the detected_day_expr() column on the frame. Equal confidences
    rank in input order. Rows with a null key (an unparsable time under DAY, a
    missing Week under WEEK) share one group.
    """
    keys = [pl.col("Species"), *(_group_expr(k) for k in spread)]
    return pl.col("Confidence").rank("ordinal", descending=True).over(keys)
