"""Continuous monitoring — devices and the readings they produce.

This is the layer that turns MomCare from a record system into a monitoring
one. Readings attach to a **pregnancy**, never directly to a patient: a heart
rate of 110 is unremarkable at 12 weeks and worth attention at 38, so a reading
without its gestational context cannot be interpreted.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from momcare_platform.core.common.models import Deactivatable, TimeStampedModel, UUIDPrimaryKeyModel


class Device(UUIDPrimaryKeyModel, Deactivatable, TimeStampedModel):
    """A wearable band, and who is currently wearing it.

    The assignment is what lets an incoming reading resolve to a patient: the
    band knows its own serial, not whose wrist it is on. Assignment is to a
    **pregnancy** rather than a patient, so a band reissued for a later
    pregnancy does not silently attach new readings to the old episode.

    ``acquisition`` records how the mother came by it. MomCare targets
    resource-constrained settings, so a device may be sold, subsidised, or lent
    by the hospital for the high-risk weeks and reclaimed afterwards — a model
    that only supports purchase would exclude the women most at risk.
    """

    STATUS_IN_STOCK = "in_stock"
    STATUS_ASSIGNED = "assigned"
    STATUS_RETURNED = "returned"
    STATUS_FAULTY = "faulty"
    STATUS_LOST = "lost"
    STATUS_CHOICES = [
        (STATUS_IN_STOCK, "In stock"),
        (STATUS_ASSIGNED, "Assigned"),
        (STATUS_RETURNED, "Returned"),
        (STATUS_FAULTY, "Faulty"),
        (STATUS_LOST, "Lost"),
    ]

    ACQUISITION_SOLD = "sold"
    ACQUISITION_LOANED = "loaned"
    ACQUISITION_SUBSIDISED = "subsidised"
    ACQUISITION_CHOICES = [
        (ACQUISITION_SOLD, "Sold"),
        (ACQUISITION_LOANED, "Loaned by the hospital"),
        (ACQUISITION_SUBSIDISED, "Subsidised"),
    ]

    serial_number = models.CharField(max_length=64, unique=True, db_index=True)
    organization = models.ForeignKey(
        "organization.Organization",
        on_delete=models.PROTECT,
        related_name="devices",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_IN_STOCK, db_index=True)

    assigned_pregnancy = models.ForeignKey(
        "patients.Pregnancy",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="devices",
    )
    assigned_at = models.DateTimeField(null=True, blank=True)
    acquisition = models.CharField(max_length=20, choices=ACQUISITION_CHOICES, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["serial_number"]
        indexes = [models.Index(fields=["organization", "status"])]
        constraints = [
            # A band cannot be on two wrists. Only assigned devices are
            # constrained; many can sit in stock unassigned.
            models.UniqueConstraint(
                fields=["assigned_pregnancy"],
                condition=models.Q(status="assigned"),
                name="one_active_device_per_pregnancy",
            ),
        ]

    def __str__(self) -> str:
        if self.assigned_pregnancy_id:
            return f"{self.serial_number} → {self.assigned_pregnancy.patient.full_name}"
        return f"{self.serial_number} ({self.get_status_display()})"

    @property
    def is_assigned(self) -> bool:
        return self.status == self.STATUS_ASSIGNED and self.assigned_pregnancy_id is not None


class VitalReading(UUIDPrimaryKeyModel):
    """One measurement, at one moment, for one pregnancy.

    Long format — a row per measurement type rather than a wide row per event —
    because the sources report at different rhythms: a band streams heart rate
    continuously while blood pressure comes from a cuff once or twice a day. A
    wide row would be mostly empty and would imply readings were taken together
    when they were not.

    Blood pressure is the exception and keeps both numbers on one row:
    140/90 is a single clinical fact, and splitting it would let a rule see a
    systolic value with no diastolic to pair it with.

    Deliberately not TimeStamped or Deactivatable. A reading is an observation
    of something that happened at ``recorded_at``; there is no meaningful
    "updated" and it is never deleted. Corrections are new readings.
    """

    TYPE_BLOOD_PRESSURE = "blood_pressure"
    TYPE_HEART_RATE = "heart_rate"
    TYPE_TEMPERATURE = "temperature"
    TYPE_CHOICES = [
        (TYPE_BLOOD_PRESSURE, "Blood pressure"),
        (TYPE_HEART_RATE, "Heart rate"),
        (TYPE_TEMPERATURE, "Temperature"),
    ]

    UNITS = {
        TYPE_BLOOD_PRESSURE: "mmHg",
        TYPE_HEART_RATE: "bpm",
        TYPE_TEMPERATURE: "°C",
    }

    # Where the number came from. Recorded on every row so that simulated data
    # can never be mistaken for a real measurement — in a monitoring system
    # that would be the most dangerous kind of quiet mistake.
    SOURCE_DEVICE = "device"
    SOURCE_MANUAL = "manual"
    SOURCE_SIMULATED = "simulated"
    SOURCE_CHOICES = [
        (SOURCE_DEVICE, "Wearable device"),
        (SOURCE_MANUAL, "Entered by staff"),
        (SOURCE_SIMULATED, "Simulated"),
    ]

    pregnancy = models.ForeignKey(
        "patients.Pregnancy",
        on_delete=models.PROTECT,
        related_name="readings",
    )
    reading_type = models.CharField(max_length=20, choices=TYPE_CHOICES, db_index=True)

    # Systolic for blood pressure; the single value for everything else.
    value = models.DecimalField(max_digits=6, decimal_places=2)
    # Diastolic — only populated for blood pressure.
    value_secondary = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

    recorded_at = models.DateTimeField(_("recorded at"), db_index=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_DEVICE, db_index=True)
    device = models.ForeignKey(
        "monitoring.Device",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="readings",
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recorded_readings",
        help_text="The staff member who entered this, for manual readings.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-recorded_at"]
        indexes = [
            # Every clinical query is "this pregnancy, this measurement, most
            # recent first". At a reading every few minutes this table grows
            # fast, and adding the index later means rebuilding it under load.
            models.Index(fields=["pregnancy", "reading_type", "-recorded_at"]),
            models.Index(fields=["pregnancy", "-recorded_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(value__gt=0),
                name="reading_value_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_reading_type_display()} {self.display_value} at {self.recorded_at:%Y-%m-%d %H:%M}"

    @property
    def unit(self) -> str:
        return self.UNITS.get(self.reading_type, "")

    @property
    def display_value(self) -> str:
        """How a clinician would write it: 140/90 mmHg, 88 bpm, 37.2 °C."""
        if self.reading_type == self.TYPE_BLOOD_PRESSURE and self.value_secondary is not None:
            return f"{self.value:.0f}/{self.value_secondary:.0f} {self.unit}"
        if self.reading_type == self.TYPE_HEART_RATE:
            return f"{self.value:.0f} {self.unit}"
        return f"{self.value:.1f} {self.unit}"

    @property
    def is_simulated(self) -> bool:
        return self.source == self.SOURCE_SIMULATED


class RiskAssessment(UUIDPrimaryKeyModel):
    """A judgement about one pregnancy at one moment.

    Rows are written only when the level **changes**, so this is a history of
    transitions rather than one row per reading — "she became high risk at
    14:32" is the fact alerts and audits need, and a row per reading would be
    millions of near-identical records.

    ``source`` is the seam a trained model plugs into. The rules engine writes
    rows with source=RULES today; a model writes the same shape with
    source=MODEL, and the portal reads both without caring which produced them
    — it only labels them honestly.

    ``findings`` is not decoration. A level with no explanation is something a
    clinician can neither act on nor overrule, and unexplained automated
    judgements are exactly what makes clinical software untrustworthy.
    """

    LEVEL_STABLE = "stable"
    LEVEL_MODERATE = "moderate"
    LEVEL_HIGH = "high"
    LEVEL_CRITICAL = "critical"
    LEVEL_CHOICES = [
        (LEVEL_STABLE, "Stable"),
        (LEVEL_MODERATE, "Moderate"),
        (LEVEL_HIGH, "High"),
        (LEVEL_CRITICAL, "Critical"),
    ]

    SOURCE_RULES = "rules"
    SOURCE_MODEL = "model"
    SOURCE_CHOICES = [
        (SOURCE_RULES, "Clinical rules"),
        (SOURCE_MODEL, "AI model"),
    ]

    pregnancy = models.ForeignKey(
        "patients.Pregnancy",
        on_delete=models.PROTECT,
        related_name="risk_assessments",
    )
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, db_index=True)
    findings = models.JSONField(
        default=list,
        help_text="Why this level was reached, each tied to the reading that caused it.",
    )

    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_RULES, db_index=True)
    engine_version = models.CharField(
        max_length=40,
        help_text="Which rules or model version produced this, so a judgement can be traced back.",
    )
    # Only a model produces these; the rules engine leaves them null rather
    # than inventing a number that would imply a confidence it does not have.
    score = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)

    assessed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # What the previous level was, so a transition reads on its own.
    previous_level = models.CharField(max_length=20, choices=LEVEL_CHOICES, blank=True)

    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="acknowledged_assessments",
    )

    class Meta:
        ordering = ["-assessed_at"]
        indexes = [
            models.Index(fields=["pregnancy", "-assessed_at"]),
            models.Index(fields=["level", "-assessed_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.pregnancy.patient.full_name} — {self.get_level_display()} ({self.source})"

    @property
    def is_actionable(self) -> bool:
        return self.level != self.LEVEL_STABLE

    @property
    def needs_acknowledgement(self) -> bool:
        """An unacknowledged non-stable assessment is one nobody has looked at."""
        return self.is_actionable and self.acknowledged_at is None

    @property
    def reasons(self) -> list[str]:
        return [f.get("detail", "") for f in self.findings]
