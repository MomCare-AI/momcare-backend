"""Pregnancy dating arithmetic.

Gestational age is computed in exactly one place because vitals, risk scoring
and alerts all interpret readings through it — two implementations that drifted
apart would mean the dashboard and the risk engine disagreeing about how
pregnant someone is.
"""

from datetime import date, timedelta

import pytest

from momcare_platform.core.common.obstetrics import (
    GestationalAge,
    calculate_gestational_age,
    edd_from_lmp,
    is_term,
)


def test_edd_is_lmp_plus_280_days():
    assert edd_from_lmp(date(2026, 2, 5)) == date(2026, 11, 12)


def test_gestational_age_at_edd_is_full_term():
    edd = date(2026, 11, 12)
    assert calculate_gestational_age(edd, on_date=edd) == GestationalAge(40, 0)


def test_gestational_age_counts_from_the_edd_backwards():
    edd = date(2026, 11, 12)
    # 12 weeks before the due date is 28 weeks pregnant.
    on = edd - timedelta(weeks=12)
    assert calculate_gestational_age(edd, on_date=on) == GestationalAge(28, 0)


def test_gestational_age_reports_part_weeks():
    edd = date(2026, 11, 12)
    on = edd - timedelta(weeks=12) + timedelta(days=3)
    age = calculate_gestational_age(edd, on_date=on)
    assert (age.weeks, age.days) == (28, 3)
    assert str(age) == "28w 3d"


@pytest.mark.parametrize(
    ("offset_days", "expected"),
    [(6, (39, 1)), (7, (39, 0)), (8, (38, 6))],
)
def test_week_boundaries(offset_days, expected):
    """Rolling over from 39w0d to 38w6d is the edge most likely to be off by one."""
    edd = date(2026, 11, 12)
    age = calculate_gestational_age(edd, on_date=edd - timedelta(days=offset_days))
    assert (age.weeks, age.days) == expected


def test_post_term_keeps_counting():
    """Past the due date is clinically significant — it must not clamp at 40w."""
    edd = date(2026, 11, 12)
    age = calculate_gestational_age(edd, on_date=edd + timedelta(days=10))
    assert age.weeks == 41
    assert age.days == 3


def test_before_conception_is_zero_not_negative():
    edd = date(2026, 11, 12)
    age = calculate_gestational_age(edd, on_date=edd - timedelta(days=400))
    assert age == GestationalAge(0, 0)


def test_no_edd_returns_none_rather_than_zero():
    """Unknown must stay visibly unknown — zero would read as a new pregnancy."""
    assert calculate_gestational_age(None) is None


def test_total_days():
    assert GestationalAge(28, 3).total_days == 199


@pytest.mark.parametrize(
    ("weeks", "term"),
    [(36, False), (37, True), (40, True)],
)
def test_is_term_boundary(weeks, term):
    assert is_term(GestationalAge(weeks, 0)) is term


def test_is_term_of_unknown_is_false():
    assert is_term(None) is False
