"""Largest-remainder percentage allocation.

Ported verbatim (same algorithm, same tie-breaking) from Neuro_RPM's
``guideline_bands.py::allocate_percentages`` — naive rounding of, say, three
even thirds gives 33+33+33=99, not 100; this hands the leftover point(s) to
whichever categories had the largest fractional remainder, so a percentage
breakdown always sums to exactly 100.
"""

from __future__ import annotations


def allocate_percentages(counts: dict[str, int], total: int) -> dict[str, int]:
    """Convert raw counts into whole-number percentages that sum to ``total``'s
    100% exactly, no matter how the fractions fall.

    Every key in ``counts`` gets an entry, including ones with a count of 0.
    Returns all-zero when ``total`` is 0 (no readings), rather than dividing
    by zero.
    """
    if not total:
        return dict.fromkeys(counts, 0)

    exact = {key: (count / total) * 100 for key, count in counts.items()}
    floors = {key: int(value) for key, value in exact.items()}
    remainder = 100 - sum(floors.values())
    order = sorted(counts, key=lambda key: exact[key] - floors[key], reverse=True)
    for key in order[:remainder]:
        floors[key] += 1
    return floors
