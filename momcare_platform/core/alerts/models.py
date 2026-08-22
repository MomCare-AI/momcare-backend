"""Alerts — the push side of monitoring.

The attention queue is *pull*: it works only while somebody is looking at a
screen. An alert is what reaches a clinician who is not. Everything here exists
to make one guarantee auditable — that a patient who crossed a clinical
threshold was brought to a named person's attention, and that if nobody
answered, somebody more senior was told.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from momcare_platform.core.alerts import escalation
from momcare_platform.core.common.models import TimeStampedModel, UUIDPrimaryKeyModel


class Alert(UUIDPrimaryKeyModel, TimeStampedModel):
    """One episode of a patient needing attention.

    Not one row per assessment. A pregnancy has **at most one open alert**, and
    a worsening assessment sharpens that alert rather than adding another —
    alert fatigue is what kills clinical alerting systems, and two rows for the
    same deteriorating patient trains people to dismiss both.
    """

    STATUS_OPEN = "open"
    STATUS_ACKNOWLEDGED = "acknowledged"
    STATUS_RESOLVED = "resolved"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_ACKNOWLEDGED, "Acknowledged"),
        (STATUS_RESOLVED, "Resolved"),
    ]
    # Acknowledged is deliberately not closed: someone has seen it, but the
    # patient is still outside range. Only a resolution ends the episode.
    LIVE_STATUSES = (STATUS_OPEN, STATUS_ACKNOWLEDGED)

    RESOLUTION_RECOVERED = "recovered"
    RESOLUTION_HANDLED = "handled"
    RESOLUTION_PREGNANCY_ENDED = "pregnancy_ended"
    RESOLUTION_CHOICES = [
        (RESOLUTION_RECOVERED, "Readings returned to range"),
        (RESOLUTION_HANDLED, "Handled by a clinician"),
        (RESOLUTION_PREGNANCY_ENDED, "Pregnancy ended"),
    ]

    pregnancy = models.ForeignKey(
        "patients.Pregnancy",
        on_delete=models.CASCADE,
        related_name="alerts",
    )
    # The assessment that currently justifies this alert. Updated in place when
    # the patient worsens, so the alert always points at the reason it is live
    # rather than at a stale first reading.
    assessment = models.ForeignKey(
        "monitoring.RiskAssessment",
        on_delete=models.PROTECT,
        related_name="alerts",
    )
    level = models.CharField(max_length=20, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_OPEN,
        db_index=True,
    )
    tier = models.PositiveSmallIntegerField(
        default=escalation.TIER_CLINICIAN,
        help_text="How far up the ladder this alert has climbed.",
    )

    raised_at = models.DateTimeField(auto_now_add=True, db_index=True)
    last_escalated_at = models.DateTimeField(null=True, blank=True)

    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="alerts_acknowledged",
    )

    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="alerts_resolved",
    )
    resolution = models.CharField(max_length=30, choices=RESOLUTION_CHOICES, blank=True)

    class Meta:
        ordering = ["-raised_at"]
        indexes = [
            models.Index(fields=["status", "-raised_at"]),
            models.Index(fields=["pregnancy", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["pregnancy"],
                condition=models.Q(status__in=("open", "acknowledged")),
                name="one_live_alert_per_pregnancy",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_status_display()} {self.level} alert for {self.pregnancy_id}"

    @property
    def is_live(self) -> bool:
        return self.status in self.LIVE_STATUSES

    @property
    def tier_label(self) -> str:
        return escalation.tier_label(self.tier)

    @property
    def next_escalation_at(self):
        """When this climbs next — None once acknowledged or at the top rung.

        Acknowledgement stops the clock: the point of escalating is to find
        somebody who will look, and somebody has.
        """
        if self.status != self.STATUS_OPEN:
            return None
        return escalation.next_escalation_at(self.level, self.raised_at, self.tier)

    @property
    def reasons(self) -> list[str]:
        return self.assessment.reasons if self.assessment_id else []


class AlertEvent(UUIDPrimaryKeyModel):
    """Append-only history of one alert's life.

    Escalation that is not written down is indistinguishable from escalation
    that never happened. This table is the answer to "who was told, when, and
    did anyone respond" — the question asked after something goes wrong, when
    the alert row's current state is no longer enough.
    """

    KIND_RAISED = "raised"
    KIND_WORSENED = "worsened"
    KIND_ESCALATED = "escalated"
    KIND_NOTIFIED = "notified"
    KIND_ACKNOWLEDGED = "acknowledged"
    KIND_RESOLVED = "resolved"
    KIND_CHOICES = [
        (KIND_RAISED, "Raised"),
        (KIND_WORSENED, "Worsened"),
        (KIND_ESCALATED, "Escalated"),
        (KIND_NOTIFIED, "Notified"),
        (KIND_ACKNOWLEDGED, "Acknowledged"),
        (KIND_RESOLVED, "Resolved"),
    ]

    alert = models.ForeignKey(Alert, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    tier = models.PositiveSmallIntegerField(null=True, blank=True)
    detail = models.CharField(max_length=255, blank=True)
    # Null for anything the system did on its own — escalation has no actor,
    # and recording one would misattribute a machine decision to a person.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["alert", "created_at"])]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} — {self.detail}"
