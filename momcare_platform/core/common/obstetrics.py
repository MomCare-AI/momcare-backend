"""Obstetric date arithmetic — the single source of truth for pregnancy dating.

Gestational age is the context that gives every other clinical number meaning:
a heart rate of 110 at 12 weeks and at 38 weeks are different events. Vitals,
risk scoring, alerts and reports will all need it, so it is computed here once
and never stored — a column would be stale the day after it was written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# Naegele's rule: a term pregnancy is 280 days (40 weeks) from the first day of
# the last menstrual period.
PREGNANCY_LENGTH_DAYS = 280
TERM_WEEKS = 40


@dataclass(frozen=True)
class GestationalAge:
    """How far along a pregnancy is, in the way clinicians write it: 28w+3d."""

    weeks: int
    days: int

    @property
    def total_days(self) -> int:
        return self.weeks * 7 + self.days

    def __str__(self) -> str:
        return f"{self.weeks}w {self.days}d"


def edd_from_lmp(lmp: date) -> date:
    """Estimated delivery date by Naegele's rule.

    Only an estimate: first-trimester ultrasound dating is more accurate and
    supersedes this in practice, which is why EDD is stored and overridable
    rather than always derived.
    """
    return lmp + timedelta(days=PREGNANCY_LENGTH_DAYS)


def calculate_gestational_age(edd: date | None, on_date: date | None = None) -> GestationalAge | None:
    """Gestational age on a given date, working backwards from the EDD.

    Derived from EDD rather than LMP so that a date corrected by ultrasound
    flows through to every downstream calculation automatically.

    Returns None when there is no EDD — an unknown gestational age must stay
    visibly unknown rather than defaulting to zero, which would read as a
    brand-new pregnancy.

    Values outside a plausible pregnancy are clamped: before conception is 0w0d,
    and a post-term pregnancy keeps counting up (42w+ is clinically meaningful).
    """
    if edd is None:
        return None

    on_date = on_date or date.today()
    days_elapsed = PREGNANCY_LENGTH_DAYS - (edd - on_date).days

    if days_elapsed < 0:
        return GestationalAge(0, 0)

    return GestationalAge(weeks=days_elapsed // 7, days=days_elapsed % 7)


def is_term(age: GestationalAge | None) -> bool:
    """37 weeks or more — the threshold below which a birth is preterm."""
    return age is not None and age.weeks >= 37


def gestational_age_long_display(age: GestationalAge | None) -> str | None:
    """A friendlier breakdown for dashboard summaries -- "7 months 2 weeks 4
    days" -- distinct from ``GestationalAge.__str__``'s plain "Xw Yd", which
    stays the clinical convention used everywhere else (Pregnancy detail,
    the risk engine, reports). Never replaces the short form -- only an
    additional display for a summary list where a lay reading is more useful
    than clinical shorthand.

    "Month" here means a 4-week (28-day) unit, not a calendar month -- the
    same informal "10 lunar months" convention behind PREGNANCY_LENGTH_DAYS
    (280 = 28 x 10) dividing evenly, and unambiguous the way a calendar
    month (28-31 days) is not. Under one month, falls back to the plain
    "Xw Yd" form -- there is nothing to break down yet.
    """
    if age is None:
        return None

    total_days = age.total_days
    if total_days < 28:
        return str(age)

    months, remainder = divmod(total_days, 28)
    weeks, days = divmod(remainder, 7)

    parts = [f"{months} month{'s' if months != 1 else ''}"]
    if weeks:
        parts.append(f"{weeks} week{'s' if weeks != 1 else ''}")
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    return " ".join(parts)


# ── Trimester and care-plan month ──────────────────────────────────────────

# Standard clinical cut-offs (ACOG): first trimester up to 13w6d, second
# 14w0d-27w6d, third from 28w0d. Defined once here so an obstetrician's
# correction is a one-line edit. NOT yet reviewed by an obstetrician.
SECOND_TRIMESTER_STARTS_WEEK = 14
THIRD_TRIMESTER_STARTS_WEEK = 28

# A care plan covers 30 days counted from day 1 of the pregnancy (EDD - 280).
# Doctors count in weeks and trimesters; the 30-day "month" is only the unit
# staff review and sign a plan in, and it deliberately does not line up with
# the trimester boundaries (week 14 falls inside plan 4, week 28 inside plan 7).
CARE_PLAN_MONTH_DAYS = 30


def trimester_for(age: GestationalAge | None) -> int | None:
    """1, 2 or 3 -- None when the gestational age is unknown (absent is not zero)."""
    if age is None:
        return None
    if age.weeks < SECOND_TRIMESTER_STARTS_WEEK:
        return 1
    if age.weeks < THIRD_TRIMESTER_STARTS_WEEK:
        return 2
    return 3


def care_plan_month(age: GestationalAge | None) -> int | None:
    """1-based 30-day block of the pregnancy. Keeps counting past term, so a
    post-term pregnancy still gets a plan. None when the age is unknown."""
    if age is None:
        return None
    return age.total_days // CARE_PLAN_MONTH_DAYS + 1


def care_plan_month_bounds(edd: date, month_number: int) -> tuple[date, date]:
    """First and last day (inclusive) of care-plan month ``month_number``."""
    day_one = edd - timedelta(days=PREGNANCY_LENGTH_DAYS)
    start = day_one + timedelta(days=(month_number - 1) * CARE_PLAN_MONTH_DAYS)
    return start, start + timedelta(days=CARE_PLAN_MONTH_DAYS - 1)


def pregnancy_week(age: GestationalAge | None) -> int | None:
    """The pregnancy week she is in: completed weeks, so 17w0d-17w6d is week 17.
    None when the gestational age is unknown."""
    return None if age is None else age.weeks


def pregnancy_week_bounds(edd: date, week_number: int) -> tuple[date, date]:
    """First and last day (inclusive) of pregnancy week ``week_number``, counted
    from day 1 of the pregnancy (EDD - 280). Trimester boundaries (14w0d, 28w0d)
    fall on week starts."""
    day_one = edd - timedelta(days=PREGNANCY_LENGTH_DAYS)
    start = day_one + timedelta(days=week_number * 7)
    return start, start + timedelta(days=6)
