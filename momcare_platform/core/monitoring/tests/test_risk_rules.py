"""The clinical rules, tested as a pure function.

These are the thresholds that decide whether a clinician is told to look at
someone. They are worth testing precisely — an off-by-one at 140 is the
difference between catching gestational hypertension and missing it.
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from momcare_platform.core.monitoring import risk_rules
from momcare_platform.core.monitoring.risk_rules import (
    LEVEL_CRITICAL,
    LEVEL_HIGH,
    LEVEL_MODERATE,
    LEVEL_STABLE,
    assess,
    highest,
)

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=dt_timezone.utc)


def reading(reading_type, value, secondary=None, *, minutes_ago=5):
    """A stand-in for a VitalReading — the rules only touch these attributes."""
    return SimpleNamespace(
        id=f"reading-{reading_type}",
        reading_type=reading_type,
        value=Decimal(str(value)),
        value_secondary=None if secondary is None else Decimal(str(secondary)),
        recorded_at=NOW - timedelta(minutes=minutes_ago),
    )


def bp(systolic, diastolic, **kwargs):
    return {"blood_pressure": reading("blood_pressure", systolic, diastolic, **kwargs)}


# ── Blood pressure — the one that matters most ───────────────────────────────


@pytest.mark.parametrize(
    ("systolic", "diastolic", "expected"),
    [
        (118, 75, LEVEL_STABLE),
        (139, 89, LEVEL_STABLE),     # just under both thresholds
        (140, 85, LEVEL_MODERATE),   # systolic alone crosses
        (130, 90, LEVEL_MODERATE),   # diastolic alone crosses
        (159, 109, LEVEL_MODERATE),
        (160, 95, LEVEL_CRITICAL),   # severe systolic
        (150, 110, LEVEL_CRITICAL),  # severe diastolic alone
        (175, 115, LEVEL_CRITICAL),
    ],
)
def test_blood_pressure_thresholds(systolic, diastolic, expected):
    """Either number crossing is enough — a rule that required both would miss
    the isolated systolic hypertension that often presents first."""
    result = assess(bp(systolic, diastolic), now=NOW)
    assert result.level == expected


def test_severe_hypertension_names_preeclampsia():
    """The finding has to tell a clinician why it matters, not just how high."""
    result = assess(bp(168, 112), now=NOW)
    detail = result.findings[0].detail.lower()
    assert "preeclampsia" in detail
    assert "168/112" in result.findings[0].detail


def test_low_blood_pressure_is_flagged_too():
    result = assess(bp(85, 55), now=NOW)
    assert result.level == LEVEL_MODERATE
    assert result.findings[0].code == "bp_low"


def test_blood_pressure_without_a_diastolic_is_ignored():
    """A half-recorded blood pressure cannot be judged, and guessing would be
    worse than declining to."""
    result = assess({"blood_pressure": reading("blood_pressure", 180, None)}, now=NOW)
    assert result.level == LEVEL_STABLE
    assert result.findings == []


# ── Temperature and heart rate ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("temp", "expected"),
    [(36.8, LEVEL_STABLE), (37.9, LEVEL_STABLE), (38.0, LEVEL_MODERATE), (39.2, LEVEL_HIGH)],
)
def test_temperature_thresholds(temp, expected):
    result = assess({"temperature": reading("temperature", temp)}, now=NOW)
    assert result.level == expected


@pytest.mark.parametrize(
    ("rate", "expected"),
    [(88, LEVEL_STABLE), (119, LEVEL_STABLE), (120, LEVEL_MODERATE), (48, LEVEL_MODERATE)],
)
def test_heart_rate_thresholds(rate, expected):
    result = assess({"heart_rate": reading("heart_rate", rate)}, now=NOW)
    assert result.level == expected


# ── Combining findings ───────────────────────────────────────────────────────


def test_the_worst_finding_wins():
    """Risk never averages away — a critical blood pressure is not softened by
    a normal temperature."""
    latest = {
        **bp(165, 108),
        "temperature": reading("temperature", 36.9),
        "heart_rate": reading("heart_rate", 84),
    }

    result = assess(latest, now=NOW)

    assert result.level == LEVEL_CRITICAL


def test_every_finding_is_kept_not_just_the_worst():
    """A clinician needs the whole picture, not only the headline."""
    latest = {**bp(145, 92), "temperature": reading("temperature", 38.4)}

    result = assess(latest, now=NOW)

    codes = {f.code for f in result.findings}
    assert codes == {"bp_raised", "fever"}


def test_a_stable_assessment_has_no_findings():
    latest = {**bp(115, 74), "heart_rate": reading("heart_rate", 82)}

    result = assess(latest, now=NOW)

    assert result.level == LEVEL_STABLE
    assert result.findings == []
    assert result.is_actionable is False


# ── Silence is a finding ─────────────────────────────────────────────────────


def test_stale_readings_are_flagged_even_when_the_values_were_normal():
    """The failure that matters most in monitoring: a patient whose band stopped
    reporting is not stable, she is unobserved — and a screen that renders that
    as calm is the one way this system could quietly fail someone."""
    latest = {**bp(118, 76, minutes_ago=60 * 20)}

    result = assess(latest, now=NOW)

    assert result.level == LEVEL_MODERATE
    assert any(f.code == "stale_readings" for f in result.findings)
    assert "not currently being monitored" in result.findings[-1].detail


def test_recent_readings_are_not_flagged_as_stale():
    result = assess({**bp(118, 76, minutes_ago=30)}, now=NOW)
    assert not any(f.code == "stale_readings" for f in result.findings)


def test_no_readings_at_all_produces_no_findings():
    """Absent data is the caller's problem to surface; the rules do not invent
    a judgement about someone they have never seen."""
    result = assess({}, now=NOW)
    assert result.level == LEVEL_STABLE
    assert result.findings == []


# ── Level ordering ───────────────────────────────────────────────────────────


def test_highest_picks_the_most_severe():
    assert highest([LEVEL_STABLE, LEVEL_CRITICAL, LEVEL_MODERATE]) == LEVEL_CRITICAL
    assert highest([LEVEL_MODERATE, LEVEL_HIGH]) == LEVEL_HIGH
    assert highest([]) == LEVEL_STABLE


def test_findings_carry_the_reading_that_caused_them():
    """An unexplained verdict is one a clinician can neither act on nor overrule."""
    result = assess(bp(150, 95), now=NOW)
    assert result.findings[0].reading_id == "reading-blood_pressure"


def test_engine_version_is_recorded():
    """Every judgement must be traceable to what produced it."""
    result = assess(bp(150, 95), now=NOW)
    assert result.engine_version == risk_rules.ENGINE_VERSION
