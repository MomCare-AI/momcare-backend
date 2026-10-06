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
    care_plan_month,
    care_plan_month_bounds,
    edd_from_lmp,
    gestational_age_long_display,
    is_term,
    pregnancy_week,
    pregnancy_week_bounds,
    trimester_for,
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
    assert age is not None
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
    assert age is not None
    assert (age.weeks, age.days) == expected


def test_post_term_keeps_counting():
    """Past the due date is clinically significant — it must not clamp at 40w."""
    edd = date(2026, 11, 12)
    age = calculate_gestational_age(edd, on_date=edd + timedelta(days=10))
    assert age is not None
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


# ── gestational_age_long_display ─────────────────────────────────────────


def test_long_display_of_none_is_none():
    assert gestational_age_long_display(None) is None


def test_under_a_month_falls_back_to_the_short_form():
    age = GestationalAge(3, 2)
    assert gestational_age_long_display(age) == "3w 2d"


def test_exactly_one_month_omits_weeks_and_days():
    age = GestationalAge(4, 0)
    assert gestational_age_long_display(age) == "1 month"


def test_full_term_is_ten_months():
    """280 days = 28 x 10 -- the whole point of the 28-day month convention
    is that full term divides evenly."""
    age = GestationalAge(40, 0)
    assert gestational_age_long_display(age) == "10 months"


def test_months_weeks_and_days_all_present():
    age = GestationalAge(30, 4)  # 214 days = 7*28 + 18 = 7 months, 2 weeks, 4 days
    assert gestational_age_long_display(age) == "7 months 2 weeks 4 days"


def test_singular_month_and_week_and_day_have_no_trailing_s():
    age = GestationalAge(5, 1)  # 36 days = 1*28 + 8 = 1 month, 1 week, 1 day
    assert gestational_age_long_display(age) == "1 month 1 week 1 day"


def test_zero_gestational_age_falls_back_to_short_form():
    assert gestational_age_long_display(GestationalAge(0, 0)) == "0w 0d"


# ── Trimester and care-plan month ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("weeks", "days", "expected"),
    [
        (0, 0, 1),
        (13, 6, 1),  # last day of the first trimester
        (14, 0, 2),  # first day of the second
        (27, 6, 2),
        (28, 0, 3),  # first day of the third
        (40, 0, 3),
        (43, 2, 3),  # post-term keeps counting in the third trimester
    ],
)
def test_trimester_boundaries(weeks, days, expected):
    assert trimester_for(GestationalAge(weeks, days)) == expected


def test_unknown_gestational_age_has_no_trimester():
    assert trimester_for(None) is None


@pytest.mark.parametrize(
    ("weeks", "days", "expected_month"),
    [
        (0, 0, 1),
        (4, 1, 1),  # day 29 -> still plan 1 (days 0-29)
        (4, 2, 2),  # day 30 -> plan 2
        (14, 0, 4),  # day 98 -> plan 4 (days 90-119): second trimester begins inside it
        (27, 6, 7),  # day 195 -> plan 7
        (28, 0, 7),  # day 196 -> plan 7: third trimester begins inside it
        (40, 0, 10),  # day 280 -> plan 10
        (42, 0, 10),  # day 294 -> still plan 10 (270-299)
        (43, 3, 11),  # day 304 -> post-term pregnancies keep getting plans
    ],
)
def test_care_plan_month_is_thirty_day_blocks_from_day_one(weeks, days, expected_month):
    assert care_plan_month(GestationalAge(weeks, days)) == expected_month


def test_unknown_gestational_age_has_no_care_plan_month():
    assert care_plan_month(None) is None


def test_care_plan_month_bounds_start_from_day_one_of_the_pregnancy():
    edd = date(2026, 11, 12)
    day_one = edd - timedelta(days=280)  # 2026-02-05

    first_start, first_end = care_plan_month_bounds(edd, 1)
    assert first_start == day_one
    assert first_end == day_one + timedelta(days=29)

    fifth_start, fifth_end = care_plan_month_bounds(edd, 5)
    assert fifth_start == day_one + timedelta(days=120)
    assert fifth_end == day_one + timedelta(days=149)


def test_consecutive_care_plan_months_do_not_overlap_or_leave_gaps():
    edd = date(2026, 11, 12)
    _, end_of_3 = care_plan_month_bounds(edd, 3)
    start_of_4, _ = care_plan_month_bounds(edd, 4)
    assert start_of_4 == end_of_3 + timedelta(days=1)


# ── Pregnancy weeks ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("weeks", "days", "expected"),
    [(0, 0, 0), (0, 6, 0), (1, 0, 1), (17, 0, 17), (17, 6, 17), (18, 0, 18), (40, 3, 40)],
)
def test_pregnancy_week_is_the_completed_weeks(weeks, days, expected):
    assert pregnancy_week(GestationalAge(weeks, days)) == expected


def test_unknown_gestational_age_has_no_pregnancy_week():
    assert pregnancy_week(None) is None


def test_pregnancy_week_bounds_run_seven_days_from_day_one():
    edd = date(2026, 11, 12)
    day_one = edd - timedelta(days=280)

    start, end = pregnancy_week_bounds(edd, 17)

    assert start == day_one + timedelta(days=17 * 7)
    assert end == start + timedelta(days=6)


def test_consecutive_pregnancy_weeks_do_not_overlap_or_leave_gaps():
    edd = date(2026, 11, 12)
    _, end_of_16 = pregnancy_week_bounds(edd, 16)
    start_of_17, _ = pregnancy_week_bounds(edd, 17)
    assert start_of_17 == end_of_16 + timedelta(days=1)


def test_trimesters_start_on_a_week_start():
    edd = date(2026, 11, 12)
    start_14, _ = pregnancy_week_bounds(edd, 14)
    assert trimester_for(calculate_gestational_age(edd, start_14)) == 2
    assert trimester_for(calculate_gestational_age(edd, start_14 - timedelta(days=1))) == 1
