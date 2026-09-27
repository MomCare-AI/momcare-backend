from django.db import models

from momcare_platform.core.common.models import TimeStampedModel, UUIDPrimaryKeyModel


class PatientAnalytics(TimeStampedModel, UUIDPrimaryKeyModel):
    """One row per (patient, calendar month) -- the only cache MomCare
    currently needs that genuinely resets every period, unlike
    ``Patient.last_monitoring_contact_at``/``last_reading_at`` (running
    values with no month boundary, denormalized directly onto Patient
    instead -- see that model's own docstring).

    ``period_month`` is always the first of the month, in the patient's own
    Location timezone -- same resolution ``core.monitoring.services.
    month_bounds`` already uses for the Staff Audit Report's monthly totals.
    Recomputed from source data on every relevant write (never incremented
    or decremented in place) -- see ``core.monitoring.signals`` -- because
    MonitoringSession/MonitoringNote are editable and backdatable, unlike
    VitalReading, so an incremental delta could drift from the truth.
    """

    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.CASCADE,
        related_name="analytics_periods",
    )
    period_month = models.DateField()
    # Total MonitoringSession.duration_seconds recorded for this patient in
    # this calendar month. Feeds the Monitoring Follow-up care activity's
    # "< 20 minutes this month" condition -- see
    # ``core.analytics.services.needs_monitoring_follow_up``.
    monitoring_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Patient Analytics"
        verbose_name_plural = "Patient Analytics"
        constraints = [
            models.UniqueConstraint(fields=["patient", "period_month"], name="unique_patient_period"),
        ]
        indexes = [
            models.Index(fields=["patient", "period_month"]),
        ]

    def __str__(self) -> str:
        return f"{self.patient.full_name} · {self.period_month:%Y-%m}"
