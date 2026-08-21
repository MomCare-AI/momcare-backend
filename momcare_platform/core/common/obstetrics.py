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
