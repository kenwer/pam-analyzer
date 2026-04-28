"""Pure functions over Detection rows."""

from .entities import Detection


def filter_top_per_aru_species(detections: list[Detection], max_per_pair: int) -> list[Detection]:
    """Top N detections per (ARU, Species), ranked by confidence desc.

    max_per_pair <= 0 disables filtering and returns the input unchanged.
    """
    if max_per_pair <= 0:
        return detections
    sorted_rows = sorted(
        detections,
        key=lambda d: (d.aru, d.species, -d.confidence),
    )
    result: list[Detection] = []
    counts: dict[tuple[str, str], int] = {}
    for d in sorted_rows:
        key = (d.aru, d.species)
        n = counts.get(key, 0)
        if n < max_per_pair:
            result.append(d)
            counts[key] = n + 1
    return result
