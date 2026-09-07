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
    """One reading event, at one moment, for one pregnancy — wide format.

    One row per check-in, not one row per measurement type: the band reports
    blood pressure, heart rate, temperature, stress, and activity together at
    the same moment, so splitting them across separate rows would only imply
    an asynchrony that does not exist here.

    ``hemoglobin`` and ``blood_glucose`` are the exception — they do not come
    from the band. Hemoglobin arrives from a lab report roughly monthly and is
    carried forward unchanged across many rows until the next test; blood
    glucose may arrive on its own, faster schedule. Both are simply null on a
    row where nothing new is known, same as any field can be.

    Every vital is nullable for the same reason: a row records whatever was
    actually known at that moment, never a guessed or carried-over value
    presented as fresh — the categorisation and scoring layers already treat
    a missing vital as "unknown", not as normal, and this must stay true here.

    Temperature is stored in Fahrenheit throughout, matching the trained model
    and every clinical category threshold — never Celsius.

    Deliberately not TimeStamped or Deactivatable. A reading is an observation
    of something that happened at ``recorded_at``; there is no meaningful
    "updated" and it is never deleted. Corrections are new readings.

    ``source`` is never inferred from ``device`` being set — a band can be
    assigned to a pregnancy while a nurse still types a separate manual
    measurement in by hand, so the two questions ("is a band assigned" and
    "did these particular numbers come from it") are independent. The caller
    declares it explicitly on every write.
    """

    SOURCE_DEVICE = "device"
    SOURCE_MANUAL = "manual"
    SOURCE_CHOICES = [
        (SOURCE_DEVICE, "Device"),
        (SOURCE_MANUAL, "Manual entry"),
    ]

    pregnancy = models.ForeignKey(
        "patients.Pregnancy",
        on_delete=models.PROTECT,
        related_name="readings",
    )

    # The 9 vitals the risk model trains and predicts on. All nullable —
    # a reading event does not have to carry every vital every time.
    age = models.PositiveSmallIntegerField(null=True, blank=True)
    systolic_bp = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    diastolic_bp = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    heart_rate = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    body_temp_f = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    hemoglobin = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    blood_glucose = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    stress_score = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    phys_activity_score = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, db_index=True)
    recorded_at = models.DateTimeField(_("recorded at"), db_index=True)
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
        help_text="The staff member whose session submitted this reading.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-recorded_at"]
        indexes = [
            # Every clinical query is "this pregnancy, most recent first".
            models.Index(fields=["pregnancy", "-recorded_at"]),
        ]

    def __str__(self) -> str:
        return f"Reading for {self.pregnancy_id} at {self.recorded_at:%Y-%m-%d %H:%M}"


class RiskAssessment(UUIDPrimaryKeyModel):
    """A judgement about one pregnancy at one moment.

    Rows are written only when the level **changes**, so this is a history of
    transitions rather than one row per reading — "she became high risk at
    14:32" is the fact alerts and audits need, and a row per reading would be
    millions of near-identical records.

    ``risk_level`` is exactly the model's 3-class scale — Low/Medium/High —
    and nothing else, including the emergency rules engine: a rule-detected
    emergency escalates urgency (see ``flagged_for_review`` and the alert-tier
    timing it drives), it never invents a 4th risk level the model cannot
    itself produce.

    ``confirmed_risk_level`` is a doctor's correction, kept separate from
    ``risk_level`` rather than overwriting it — the original automated
    judgement is never erased, even when it turns out to be wrong.
    ``review_status`` names the three states of that process: unreviewed
    (default, and the common permanent case for most assessments), confirmed
    (a doctor agreed), or corrected (a doctor did not).
    """

    LEVEL_LOW = "low"
    LEVEL_MEDIUM = "medium"
    LEVEL_HIGH = "high"
    LEVEL_CHOICES = [
        (LEVEL_LOW, "Low"),
        (LEVEL_MEDIUM, "Medium"),
        (LEVEL_HIGH, "High"),
    ]

    REVIEW_UNREVIEWED = "unreviewed"
    REVIEW_CONFIRMED = "confirmed"
    REVIEW_CORRECTED = "corrected"
    REVIEW_STATUS_CHOICES = [
        (REVIEW_UNREVIEWED, "Unreviewed"),
        (REVIEW_CONFIRMED, "Confirmed"),
        (REVIEW_CORRECTED, "Corrected"),
    ]

    pregnancy = models.ForeignKey(
        "patients.Pregnancy",
        on_delete=models.PROTECT,
        related_name="risk_assessments",
    )
    # The exact reading this judgement was computed from — lets a single
    # query return the assessment and the vitals behind it together, instead
    # of digging a reading_id out of `findings` and querying again. Nullable
    # only because a stale-readings finding can fire with no reading at all.
    reading = models.ForeignKey(
        "monitoring.VitalReading",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="risk_assessments",
    )
    risk_level = models.CharField(max_length=20, choices=LEVEL_CHOICES, db_index=True)
    # What is actually acted on. Equal to risk_level until an escalation rule
    # overrides it (e.g. a low-confidence or region-specific rule bumping a
    # Medium up) — those rules read the trained model's confidence, so until
    # that model lands this always equals risk_level. Kept separate from
    # risk_level so the model's raw answer is never silently overwritten.
    final_risk_level = models.CharField(max_length=20, choices=LEVEL_CHOICES, db_index=True)

    # The clinical label for each raw vital on the linked reading — "Stage 2",
    # "Mild Anemia" — computed alongside risk_level, not a verdict on their
    # own. Blank, never guessed, when the corresponding vital is null.
    bp_category = models.CharField(max_length=32, blank=True)
    heart_rate_category = models.CharField(max_length=32, blank=True)
    temperature_category = models.CharField(max_length=32, blank=True)
    glucose_category = models.CharField(max_length=32, blank=True)
    hemoglobin_category = models.CharField(max_length=32, blank=True)

    # Only the risk engine produces this table today; confidence being null is
    # itself the signal that the row came from rules rather than a trained
    # model, so no separate "source" column is needed to tell them apart.
    confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)

    assessed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # What the previous risk_level was, so a transition reads on its own.
    previous_risk_level = models.CharField(max_length=20, choices=LEVEL_CHOICES, blank=True)

    # Set whenever this assessment was flagged for review — either the
    # confidence-threshold check, or the Africa+Medium rule (once wired in).
    # Separate from verified_at/by below: this records whether the flag was
    # raised at all, not whether it was later reviewed. Generic on purpose —
    # whoever ends up notified (doctor, nurse, care manager) depends on the
    # alert-escalation tier, not on this field.
    flagged_for_review = models.BooleanField(default=False)

    # The doctor's real, confirmed answer — never overwrites risk_level.
    confirmed_risk_level = models.CharField(max_length=20, choices=LEVEL_CHOICES, blank=True)
    # Set only together with confirmed_risk_level, by the same action — there
    # is no "seen but not confirmed" state. review_status is derived from
    # comparing confirmed_risk_level to final_risk_level at that moment.
    review_status = models.CharField(
        max_length=20,
        choices=REVIEW_STATUS_CHOICES,
        default=REVIEW_UNREVIEWED,
        db_index=True,
    )

    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verified_assessments",
    )

    class Meta:
        ordering = ["-assessed_at"]
        indexes = [
            models.Index(fields=["pregnancy", "-assessed_at"]),
            models.Index(fields=["risk_level", "-assessed_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.pregnancy.patient.full_name} — {self.get_risk_level_display()}"

    @property
    def is_actionable(self) -> bool:
        return self.final_risk_level != self.LEVEL_LOW

    @property
    def needs_review(self) -> bool:
        """An unreviewed non-low assessment is one no doctor has confirmed or corrected."""
        return self.is_actionable and self.review_status == self.REVIEW_UNREVIEWED
