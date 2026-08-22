"""When an unanswered alert should climb, and to whom.

A pure module, like ``monitoring.risk_rules``: no database, no framework, no
side effects. The policy is the part most likely to be argued over by a
clinician reviewing this system, so it is kept in one readable place rather
than scattered through the service layer.

The escalation ladder answers a single question — *who else needs to know that
nobody has answered?* — and it climbs on a clock, because the failure this
guards against is not a bad alert but an unread one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# ── The ladder ───────────────────────────────────────────────────────────────
# Deliberately short. Each rung must be a real person who can act; a tier that
# reaches an unattended inbox is worse than no tier, because it looks like
# escalation happened.

TIER_CLINICIAN = 1  # the clinician assigned to this pregnancy
TIER_WARD = 2  # every active clinician at the hospital
TIER_ADMIN = 3  # the hospital administrator — the end of the ladder

TIER_LABELS = {
    TIER_CLINICIAN: "Assigned clinician",
    TIER_WARD: "Clinical team",
    TIER_ADMIN: "Hospital administrator",
}

MAX_TIER = TIER_ADMIN


@dataclass(frozen=True)
class Policy:
    """How long an alert may sit unanswered at each rung, in minutes."""

    to_ward: int
    to_admin: int


# Intervals scale with severity. A critical alert nobody answers within five
# minutes is the exact scenario this whole platform exists to catch, so it
# climbs fast; a moderate one has hours of clinical slack and climbing quickly
# would only train people to ignore the channel.
#
# These intervals are a starting point drawn from how obstetric escalation is
# usually described. Like the risk thresholds, they need an obstetrician's
# review before this system is used in care.
POLICIES: dict[str, Policy] = {
    "critical": Policy(to_ward=5, to_admin=15),
    "high": Policy(to_ward=15, to_admin=45),
    "moderate": Policy(to_ward=60, to_admin=180),
}

# Stable never raises an alert, so it has no policy and no ladder.
DEFAULT_POLICY = POLICIES["moderate"]


def policy_for(level: str) -> Policy:
    return POLICIES.get(level, DEFAULT_POLICY)


def due_tier(level: str, raised_at: datetime, now: datetime) -> int:
    """Which rung this alert should be on by now.

    Computed from the clock rather than accumulated by repeated sweeps, so a
    scheduler that missed three runs still arrives at the right tier instead of
    escalating one rung per late run.
    """
    policy = policy_for(level)
    waited = now - raised_at

    if waited >= timedelta(minutes=policy.to_admin):
        return TIER_ADMIN
    if waited >= timedelta(minutes=policy.to_ward):
        return TIER_WARD
    return TIER_CLINICIAN


def next_escalation_at(level: str, raised_at: datetime, tier: int) -> datetime | None:
    """When this alert climbs next — None once it is at the top of the ladder.

    Shown in the portal so a clinician can see the deadline they are working
    against, rather than discovering it when their supervisor is paged.
    """
    policy = policy_for(level)
    if tier <= TIER_CLINICIAN:
        return raised_at + timedelta(minutes=policy.to_ward)
    if tier == TIER_WARD:
        return raised_at + timedelta(minutes=policy.to_admin)
    return None


def tier_label(tier: int) -> str:
    return TIER_LABELS.get(tier, f"Tier {tier}")
