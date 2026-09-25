"""Largest-remainder percentage allocation, and per-metric display rounding.

Both ported from Neuro_RPM's ``guideline_bands.py`` (``allocate_percentages``
and ``METRIC_ROUNDING``/``round_metric_value``) — pure display-formatting
rules, no clinical meaning of their own, kept together since both exist only
to make a statistics/summary response readable.
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


# Decimal places for average/min/max display, per vital -- 0 means rounded to
# the nearest whole number and returned as an int (e.g. 121, not 121.0);
# anything higher means rounded to that many decimal places, returned as a
# float. Ported from Neuro_RPM's own METRIC_ROUNDING (systolic/diastolic/
# heart_rate/mean_arterial_pressure/oxygen -> 0, weight/glucose/temperature ->
# 1) and extended for the two vitals Neuro_RPM doesn't have (hemoglobin,
# MomCare's own; stress/activity scores, MomCare's own self-report scale) --
# both get 1 decimal, matching weight/glucose's "continuous measurement" tier
# rather than the "simple vital-sign count" tier BP/HR fall into.
METRIC_ROUNDING: dict[str, int] = {
    "systolic_bp": 0,
    "diastolic_bp": 0,
    "heart_rate": 0,
    "body_temp_f": 1,
    "blood_glucose": 1,
    "hemoglobin": 1,
    "stress_score": 1,
    "phys_activity_score": 1,
}


def round_metric_value(metric: str, value: float) -> float | int:
    """Round ``value`` for display, using ``metric``'s own decimal rule.

    Unknown metrics default to 1 decimal place, same fallback Neuro_RPM uses.
    """
    decimals = METRIC_ROUNDING.get(metric, 1)
    rounded = round(value, decimals)
    return int(rounded) if decimals == 0 else rounded
