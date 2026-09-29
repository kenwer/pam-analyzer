"""Pure functions over detection rows."""

import polars as pl


def top_per_aru_species(frame: pl.DataFrame, max_per_pair: int) -> pl.DataFrame:
    """Top N rows per (ARU, Species) by Confidence, in the input's row order.

    The stable sort keeps earlier rows ahead on equal confidence.
    max_per_pair <= 0 returns *frame* unchanged.
    """
    if max_per_pair <= 0:
        return frame
    return (
        frame.with_row_index("__pos")
        .sort("Confidence", descending=True, maintain_order=True)
        .filter(pl.int_range(pl.len()).over(["ARU", "Species"]) < max_per_pair)
        .sort("__pos")
        .drop("__pos")
    )
