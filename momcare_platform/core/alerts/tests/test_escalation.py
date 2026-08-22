"""The escalation policy, tested as a pure function.

These intervals decide how long a deteriorating patient can go unnoticed
before somebody more senior is told. Worth testing exactly: an off-by-one at
the boundary is the difference between escalating on time and not at all.
"""

from datetime import datetime, timedelta, timezone as dt_timezone

import pytest

from momcare_platform.core.alerts.escalation import (
    MAX_TIER,
    TIER_ADMIN,
    TIER_CLINICIAN,
    TIER_WARD,
    due_tier,
    next_escalation_at,
    policy_for,
    tier_label,
)

RAISED = datetime(2026, 8, 22, 12, 0, tzinfo=dt_timezone.utc)


def at(minutes: int) -> datetime:
    return RAISED + timedelta(minutes=minutes)


# -- The ladder climbs on a clock ---------------------------------------------


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (0, TIER_CLINICIAN),
        (4, TIER_CLINICIAN),
        (5, TIER_WARD),  # the boundary itself escalates
        (14, TIER_WARD),
        (15, TIER_ADMIN),
        (600, TIER_ADMIN),  # never climbs past the top
    ],
)
def test_critical_climbs_fastest(minutes, expected):
    """Five minutes, then fifteen. A critical alert nobody answers is the exact
    scenario this platform exists to catch."""
    assert due_tier("critical", RAISED, at(minutes)) == expected


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(14, TIER_CLINICIAN), (15, TIER_WARD), (44, TIER_WARD), (45, TIER_ADMIN)],
)
def test_high_climbs_slower(minutes, expected):
    assert due_tier("high", RAISED, at(minutes)) == expected


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(59, TIER_CLINICIAN), (60, TIER_WARD), (179, TIER_WARD), (180, TIER_ADMIN)],
)
def test_moderate_has_hours_of_slack(minutes, expected):
    """Climbing a moderate alert quickly would only train people to ignore the
    channel, which costs the critical ones their audience."""
    assert due_tier("moderate", RAISED, at(minutes)) == expected


def test_severity_orders_the_deadlines():
    """A stricter level must never wait longer than a milder one."""
    critical, high, moderate = policy_for("critical"), policy_for("high"), policy_for("moderate")
    assert critical.to_ward < high.to_ward < moderate.to_ward
    assert critical.to_admin < high.to_admin < moderate.to_admin


# -- Lateness must not compound ------------------------------------------------


def test_a_late_sweep_lands_on_the_right_rung_not_one_step_up():
    """The tier is computed from the clock, not accumulated.

    A scheduler that missed an hour of runs must arrive at the correct tier on
    its next run. Stepping up once per missed run would mean a long outage
    silently under-escalated every alert it touched.
    """
    assert due_tier("critical", RAISED, at(90)) == TIER_ADMIN
    # And the same answer whatever tier it is currently sitting on.
    assert due_tier("critical", RAISED, at(90)) == due_tier("critical", RAISED, at(90))


def test_an_unknown_level_falls_back_rather_than_crashing():
    """A level this policy has never heard of - a future model emitting its own
    vocabulary - must still escalate on some schedule rather than raising."""
    assert due_tier("catastrophic", RAISED, at(0)) == TIER_CLINICIAN
    assert due_tier("catastrophic", RAISED, at(200)) == TIER_ADMIN


# -- The deadline shown to a clinician -----------------------------------------


def test_next_escalation_is_the_deadline_for_the_current_rung():
    assert next_escalation_at("critical", RAISED, TIER_CLINICIAN) == at(5)
    assert next_escalation_at("critical", RAISED, TIER_WARD) == at(15)


def test_the_top_rung_has_no_next_deadline():
    """Nothing above the hospital administrator, so the interface must show a
    deadline that does not exist as absent rather than as a date."""
    assert next_escalation_at("critical", RAISED, TIER_ADMIN) is None
    assert TIER_ADMIN == MAX_TIER


def test_every_tier_has_a_human_readable_label():
    for tier in (TIER_CLINICIAN, TIER_WARD, TIER_ADMIN):
        assert tier_label(tier)
        assert not tier_label(tier).startswith("Tier ")
