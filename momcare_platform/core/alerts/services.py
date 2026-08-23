"""Raising, escalating, and closing alerts.

The rule this module enforces, above every other: **a live alert always points
at a named recipient.** An alert nobody owns is the failure that alerting was
supposed to prevent, so when a tier has no one in it the alert climbs rather
than going quiet.
"""

from __future__ import annotations

import logging
from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from momcare_platform.core.alerts import escalation
from momcare_platform.core.alerts.models import Alert, AlertEvent
from momcare_platform.core.common.mail import send_alert_notification

logger = logging.getLogger(__name__)

# Anything above stable is worth somebody's time.
ACTIONABLE_LEVELS = ("moderate", "high", "critical")

_SEVERITY = {"stable": 0, "moderate": 1, "high": 2, "critical": 3}


def _worse(new_level: str, old_level: str) -> bool:
    return _SEVERITY.get(new_level, 0) > _SEVERITY.get(old_level, 0)


# -- Recipients ---------------------------------------------------------------


def _organization_of(alert: Alert):
    return alert.pregnancy.patient.location.organization


def recipients_for_tier(alert: Alert, tier: int) -> list:
    """Who is told at this rung. May legitimately be empty - see ``notify``."""
    from momcare_platform.core.users.models import User  # noqa: PLC0415

    organization = _organization_of(alert)

    if tier == escalation.TIER_CLINICIAN:
        staff = alert.pregnancy.assigned_staff
        # A departed clinician is soft-deleted, so the pregnancy still *looks*
        # assigned. Treating that as a valid recipient would route a critical
        # alert to nobody and mark it delivered.
        if staff and staff.is_active and staff.user and staff.user.is_active:
            return [staff.user]
        return []

    if tier == escalation.TIER_WARD:
        return list(
            User.objects.filter(
                organization=organization,
                is_active=True,
                role__code__in=[
                    settings.ROLE_PROVIDER,
                    settings.ROLE_NURSE,
                    settings.ROLE_CARE_MANAGER,
                ],
            ),
        )

    return list(
        User.objects.filter(
            organization=organization,
            is_active=True,
            role__code=settings.ROLE_HOSPITAL_ADMIN,
        ),
    )


def notify(alert: Alert, tier: int) -> int:
    """Tell everyone at this rung, and write down that we did.

    Returns how many people were reached. Zero is recorded explicitly rather
    than passed over: "nobody was on this rung" is exactly the finding an
    incident review needs, and it is why an empty tier escalates immediately
    instead of waiting out the clock.
    """
    recipients = recipients_for_tier(alert, tier)

    if not recipients:
        AlertEvent.objects.create(
            alert=alert,
            kind=AlertEvent.KIND_NOTIFIED,
            tier=tier,
            detail=f"No recipient at {escalation.tier_label(tier).lower()} - escalating.",
        )
        return 0

    for user in recipients:
        # Best-effort, like every other send in this system: a mail outage must
        # not roll back the alert. The in-portal alert is the primary channel;
        # email is a second attempt at reaching the same person.
        send_alert_notification(alert, user, tier)

    names = ", ".join(u.get_full_name() or u.email for u in recipients[:3])
    if len(recipients) > 3:
        names += f" and {len(recipients) - 3} more"

    AlertEvent.objects.create(
        alert=alert,
        kind=AlertEvent.KIND_NOTIFIED,
        tier=tier,
        detail=f"{escalation.tier_label(tier)}: {names}",
    )
    return len(recipients)


# -- Raising and closing ------------------------------------------------------


@transaction.atomic
def sync_alert_for(assessment) -> Alert | None:
    """Bring the alert state into line with a new assessment.

    Called immediately after scoring, so a dangerous reading raises its alert in
    the same request that recorded it rather than whenever a scheduler wakes.

    Three outcomes:

    - The patient became actionable and has no live alert -> raise one.
    - The patient worsened while an alert is live -> sharpen it and re-notify,
      resetting acknowledgement, because a clinician who accepted "moderate"
      has not accepted "critical".
    - The patient returned to stable -> resolve the live alert as recovered.
    """
    pregnancy = assessment.pregnancy
    live = Alert.objects.filter(pregnancy=pregnancy, status__in=Alert.LIVE_STATUSES).first()

    if assessment.level not in ACTIONABLE_LEVELS:
        if live:
            _close(
                live,
                resolution=Alert.RESOLUTION_RECOVERED,
                detail=f"Readings returned to {assessment.level}.",
            )
        return None

    if live is None:
        return _raise(pregnancy, assessment)

    if _worse(assessment.level, live.level):
        return _worsen(live, assessment)

    # Same or improved but still actionable: the alert already covers it. A new
    # row here would be the alert fatigue this design exists to avoid.
    live.assessment = assessment
    live.save(update_fields=["assessment", "updated_at"])
    return live


def _raise(pregnancy, assessment) -> Alert:
    alert = Alert.objects.create(
        pregnancy=pregnancy,
        assessment=assessment,
        level=assessment.level,
        tier=escalation.TIER_CLINICIAN,
    )
    AlertEvent.objects.create(
        alert=alert,
        kind=AlertEvent.KIND_RAISED,
        tier=alert.tier,
        detail=assessment.reasons[0] if assessment.reasons else assessment.level,
    )
    _notify_or_climb(alert)
    return alert


def _worsen(alert: Alert, assessment) -> Alert:
    previous = alert.level
    alert.level = assessment.level
    alert.assessment = assessment
    # A worse patient is a new question. An acknowledgement of the milder state
    # must not silence the escalation clock for the severe one.
    alert.status = Alert.STATUS_OPEN
    alert.acknowledged_at = None
    alert.acknowledged_by = None
    alert.save(
        update_fields=[
            "level",
            "assessment",
            "status",
            "acknowledged_at",
            "acknowledged_by",
            "updated_at",
        ],
    )
    reason = assessment.reasons[0] if assessment.reasons else ""
    AlertEvent.objects.create(
        alert=alert,
        kind=AlertEvent.KIND_WORSENED,
        tier=alert.tier,
        detail=f"{previous} -> {assessment.level}. {reason}".strip(),
    )
    _notify_or_climb(alert)
    return alert


def _notify_or_climb(alert: Alert) -> None:
    """Notify the current tier; climb straight past it if nobody is there.

    Bounded by the height of the ladder, so an organization with no staff at
    all ends at the top rung with the emptiness recorded, rather than looping.
    """
    while alert.tier <= escalation.MAX_TIER:
        if notify(alert, alert.tier):
            return
        if alert.tier == escalation.MAX_TIER:
            logger.error(
                "Alert %s has no recipient at any tier for organization %s",
                alert.id,
                _organization_of(alert).id,
            )
            return
        alert.tier += 1
        alert.last_escalated_at = timezone.now()
        alert.save(update_fields=["tier", "last_escalated_at", "updated_at"])
        AlertEvent.objects.create(
            alert=alert,
            kind=AlertEvent.KIND_ESCALATED,
            tier=alert.tier,
            detail=f"Escalated to {escalation.tier_label(alert.tier).lower()} - previous tier empty.",
        )


def _close(alert: Alert, *, resolution: str, detail: str, actor=None) -> Alert:
    alert.status = Alert.STATUS_RESOLVED
    alert.resolution = resolution
    alert.resolved_at = timezone.now()
    alert.resolved_by = actor
    alert.save(
        update_fields=["status", "resolution", "resolved_at", "resolved_by", "updated_at"],
    )
    AlertEvent.objects.create(
        alert=alert,
        kind=AlertEvent.KIND_RESOLVED,
        tier=alert.tier,
        detail=detail,
        actor=actor,
    )
    return alert


# -- Responding ---------------------------------------------------------------


@transaction.atomic
def acknowledge_alert(alert: Alert, user) -> Alert:
    """Record that a named person has seen this, and stop the clock.

    Acknowledging does not close the alert. The patient is still outside range;
    what has changed is that somebody is now accountable for her.
    """
    if alert.status != Alert.STATUS_OPEN:
        return alert

    alert.status = Alert.STATUS_ACKNOWLEDGED
    alert.acknowledged_at = timezone.now()
    alert.acknowledged_by = user
    alert.save(update_fields=["status", "acknowledged_at", "acknowledged_by", "updated_at"])
    AlertEvent.objects.create(
        alert=alert,
        kind=AlertEvent.KIND_ACKNOWLEDGED,
        tier=alert.tier,
        detail=f"Acknowledged by {user.get_full_name() or user.email}.",
        actor=user,
    )
    return alert


@transaction.atomic
def resolve_alert(alert: Alert, user, resolution: str = Alert.RESOLUTION_HANDLED) -> Alert:
    if not alert.is_live:
        return alert
    return _close(
        alert,
        resolution=resolution,
        detail=f"Closed by {user.get_full_name() or user.email}.",
        actor=user,
    )


# -- The sweep ----------------------------------------------------------------


def escalate_due_alerts(now: datetime | None = None) -> int:
    """Climb every open alert that has waited long enough. Returns how many moved.

    Idempotent, and safe to run late: the target tier is computed from the
    clock, so a sweep that missed three runs lands on the correct rung instead
    of stepping up once per late run.

    Only ``open`` alerts are considered - acknowledgement is what stops the
    ladder, because somebody has taken responsibility.
    """
    now = now or timezone.now()
    moved = 0

    candidates = list(
        Alert.objects.filter(status=Alert.STATUS_OPEN)
        .select_related(
            "pregnancy__patient__location__organization",
            "pregnancy__assigned_staff__user",
            "assessment",
        )
        .values_list("pk", flat=True),
    )

    for pk in candidates:
        with transaction.atomic():
            # Locked, and skipped if another sweep already holds it. Two runs
            # overlapping — a slow database, a late cron firing on top of the
            # previous one, or a second instance — would otherwise both read the
            # same alert at the same tier, both escalate it, and both notify:
            # duplicate emails and a history that records the climb twice.
            alert = (
                # of=("self",) locks the alert row only. Without it Postgres
                # refuses: select_related spans a nullable foreign key, which is
                # an outer join, and FOR UPDATE cannot apply to its nullable side.
                Alert.objects.select_for_update(skip_locked=True, of=("self",))
                .select_related(
                    "pregnancy__patient__location__organization",
                    "pregnancy__assigned_staff__user",
                    "assessment",
                )
                .filter(pk=pk, status=Alert.STATUS_OPEN)
                .first()
            )
            if alert is None:
                continue

            target = escalation.due_tier(alert.level, alert.raised_at, now)
            if target <= alert.tier:
                continue

            alert.tier = target
            alert.last_escalated_at = now
            alert.save(update_fields=["tier", "last_escalated_at", "updated_at"])
            AlertEvent.objects.create(
                alert=alert,
                kind=AlertEvent.KIND_ESCALATED,
                tier=target,
                detail=f"No response - escalated to {escalation.tier_label(target).lower()}.",
            )
            notify(alert, target)
        moved += 1

    return moved
