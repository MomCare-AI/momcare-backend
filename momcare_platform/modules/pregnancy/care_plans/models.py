"""Monthly Care Plan -- see docs/design/2026-10-05-care-plan-design.md.

One ``CarePlan`` per pregnancy per 30-day block counted from day 1 of the
pregnancy (EDD - 280). The plan itself is a shell: nutrition and exercise are
append-only ``CarePlanSectionVersion`` rows written by the generator,
medications and notes are staff-written rows, and doctor edits to the
generated sections live in ``CarePlanAdjustment`` -- a layer applied on top of
the newest version, so regenerating never destroys a doctor's change.

Nothing here is ever physically deleted: staff "delete" means deactivate
(``Deactivatable``), exactly like the rest of the clinical record.

Weekly structure (docs/design/2026-10-06-weekly-care-plan-design.md): nutrition and exercise
are generated per pregnancy week (``CareWeek``); ``ReadingAdvice`` is the short per-reading layer.

Tenancy: every row reaches ``Organization`` through
``care_plan__pregnancy__patient__location__organization`` (``PlanCorrection``
and ``HospitalPreference`` carry ``organization`` directly). RLS policies live
in ``organization/migrations/0032_care_plan_row_level_security.py``.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from momcare_platform.core.common.models import Deactivatable, TimeStampedModel, UUIDPrimaryKeyModel

SECTION_NUTRITION = "nutrition"
SECTION_EXERCISE = "exercise"
SECTION_CHOICES = [(SECTION_NUTRITION, "Nutrition"), (SECTION_EXERCISE, "Exercise")]


class CarePlan(UUIDPrimaryKeyModel, TimeStampedModel):
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_REVIEWED = "reviewed"
    STATUS_FINALIZED = "finalized"
    STATUS_CHOICES = [
        (STATUS_IN_PROGRESS, "In progress"),
        (STATUS_REVIEWED, "Reviewed"),
        (STATUS_FINALIZED, "Finalized"),
    ]

    pregnancy = models.ForeignKey("patients.Pregnancy", on_delete=models.PROTECT, related_name="care_plans")
    # 1-based 30-day block; derived from the EDD at creation and stored only so
    # the one-plan-per-month rule can be a database constraint. period_start/
    # period_end are likewise a snapshot -- if the EDD is later corrected by
    # ultrasound the existing plans keep the dates they were written for.
    month_number = models.PositiveSmallIntegerField()
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_IN_PROGRESS, db_index=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    finalized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    finalized_at = models.DateTimeField(null=True, blank=True)

    # The state the newest versions were built from (see state.CareState) and
    # its hash, so "has anything changed?" is one comparison, not a rebuild.
    current_state = models.JSONField(default=dict, blank=True)
    current_state_key = models.CharField(max_length=64, blank=True)
    # What the latest evaluation did: a weekly plan was written ("new"), or the week's
    # plan was kept ("continued"). One of OUTCOME_*.
    OUTCOME_NEW = "new"
    OUTCOME_CONTINUED = "continued"
    last_outcome = models.CharField(max_length=12, blank=True)
    last_evaluated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-month_number"]
        constraints = [
            models.UniqueConstraint(fields=["pregnancy", "month_number"], name="uniq_careplan_pregnancy_month"),
        ]

    def __str__(self) -> str:
        return f"Care plan {self.month_number} · {self.pregnancy_id}"


class CareWeek(UUIDPrimaryKeyModel, TimeStampedModel):
    """One pregnancy week's nutrition and exercise plan, inside a monthly CarePlan.

    A week is a pregnancy week (week 17 = 17w0d-17w6d, counted from day 1). The
    monthly plan that holds it is the one containing the week's first day, so a
    week that straddles a month edge is never split. ``progress`` is the summary
    of how she has been doing, computed by code from her readings -- never by a
    model. The trackers below drive the per-reading rule: a worse-than-plan
    reading earns quick advice, and only a worse state that persists re-plans the
    week (see ``services.process_reading``).
    """

    care_plan = models.ForeignKey(CarePlan, on_delete=models.CASCADE, related_name="weeks")
    week_number = models.PositiveSmallIntegerField()
    week_start = models.DateField()
    week_end = models.DateField()
    progress = models.JSONField(default=dict, blank=True)  # {"facts": ..., "text": ...}
    # The state this week's plan was written for, and what later readings are compared with.
    baseline_state = models.JSONField(default=dict, blank=True)
    baseline_state_key = models.CharField(max_length=64, blank=True)
    # The run of worse-than-plan readings that may turn into a re-plan.
    worse_since = models.DateTimeField(null=True, blank=True)
    worse_readings = models.PositiveSmallIntegerField(default=0)
    # The state the latest advice was written for, so identical readings don't each cost a model call.
    last_advice_state_key = models.CharField(max_length=64, blank=True)
    replans = models.PositiveSmallIntegerField(default=0)
    last_replan_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-week_number"]
        constraints = [
            models.UniqueConstraint(fields=["care_plan", "week_number"], name="uniq_careweek_plan_week"),
        ]

    def __str__(self) -> str:
        return f"Week {self.week_number} of care plan {self.care_plan_id}"


class CarePlanSectionVersion(UUIDPrimaryKeyModel):
    """One generated nutrition or exercise plan for a week. Append-only: the newest
    row per (care_plan, section) is the current one; older rows are the history
    (a mid-week re-plan adds a row to the same week)."""

    care_plan = models.ForeignKey(CarePlan, on_delete=models.CASCADE, related_name="versions")
    week = models.ForeignKey(CareWeek, null=True, blank=True, on_delete=models.CASCADE, related_name="versions")
    section = models.CharField(max_length=20, choices=SECTION_CHOICES)
    content = models.JSONField()
    # The CareState dict this version was built from -- so a month-old plan
    # still shows the allergies and vitals it was actually written for.
    inputs = models.JSONField(default=dict)
    state_key = models.CharField(max_length=64)
    source_reading = models.ForeignKey(
        "monitoring.VitalReading", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    model_name = models.CharField(max_length=200, blank=True)
    # True when the model failed or stayed invalid and the safe generic
    # baseline was stored instead. The patient never sees an error either way.
    is_fallback = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["care_plan", "section", "-created_at"])]


class ReadingAdvice(UUIDPrimaryKeyModel):
    """Short, immediate advice for one reading that is medium/high risk or worse than
    the week's plan. It never replaces the weekly plan. ``content``:
    ``{"tips": [...], "contact_care_team": bool, "sources": [...]}``."""

    care_plan = models.ForeignKey(CarePlan, on_delete=models.CASCADE, related_name="advice")
    week = models.ForeignKey(CareWeek, null=True, blank=True, on_delete=models.CASCADE, related_name="advice")
    reading = models.ForeignKey(
        "monitoring.VitalReading", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    risk_level = models.CharField(max_length=20)
    state_key = models.CharField(max_length=64)
    content = models.JSONField()
    model_name = models.CharField(max_length=200, blank=True)
    is_fallback = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["care_plan", "-created_at"])]


class CarePlanAdjustment(UUIDPrimaryKeyModel, Deactivatable, TimeStampedModel):
    """A staff addition, edit or removal on a generated section."""

    ACTION_ADD = "add"
    ACTION_EDIT = "edit"
    ACTION_REMOVE = "remove"
    ACTION_CHOICES = [(ACTION_ADD, "Add"), (ACTION_EDIT, "Edit"), (ACTION_REMOVE, "Remove")]

    care_plan = models.ForeignKey(CarePlan, on_delete=models.CASCADE, related_name="adjustments")
    section = models.CharField(max_length=20, choices=SECTION_CHOICES)
    # The standard key of the item this adjusts (a generated item for edit/
    # remove, a new one for add). A removed key is never offered again for this
    # patient, whatever the generator later says.
    item_key = models.CharField(max_length=80)
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    # Which list the item belongs to ("meals", "foods_to_avoid", "activities", ...).
    list_name = models.CharField(max_length=40)
    content = models.JSONField(default=dict, blank=True)
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["care_plan", "section", "is_active"])]


class CarePlanMedication(UUIDPrimaryKeyModel, Deactivatable, TimeStampedModel):
    """Written only by a provider -- the AI never writes or mentions medicines."""

    care_plan = models.ForeignKey(CarePlan, on_delete=models.CASCADE, related_name="medications")
    text = models.TextField()
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["created_at"]


class CarePlanNote(UUIDPrimaryKeyModel, Deactivatable, TimeStampedModel):
    """A note written for the patient -- always visible to her ("Notes for the
    patient"). Private clinical remarks belong in Clinical Notes
    (``MonitoringNote``), which stays staff-only."""

    care_plan = models.ForeignKey(CarePlan, on_delete=models.CASCADE, related_name="notes")
    text = models.TextField()
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["created_at"]


class PlanCorrection(UUIDPrimaryKeyModel):
    """Append-only JSON log of every staff edit to a generated section -- the raw
    material for hospital preferences. Written automatically; no screen."""

    ACTION_REPLACE = "replace"
    ACTION_REMOVE = "remove"
    ACTION_ADD = "add"
    ACTION_CHOICES = [(ACTION_REPLACE, "Replace"), (ACTION_REMOVE, "Remove"), (ACTION_ADD, "Add")]

    organization = models.ForeignKey("organization.Organization", on_delete=models.CASCADE, related_name="+")
    patient = models.ForeignKey("patients.Patient", on_delete=models.CASCADE, related_name="+")
    pregnancy = models.ForeignKey("patients.Pregnancy", on_delete=models.CASCADE, related_name="+")
    care_plan = models.ForeignKey(CarePlan, on_delete=models.CASCADE, related_name="corrections")
    region = models.CharField(max_length=20, blank=True)
    trimester = models.PositiveSmallIntegerField(null=True, blank=True)
    section = models.CharField(max_length=20, choices=SECTION_CHOICES)
    item_key = models.CharField(max_length=80)
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    original = models.JSONField(default=dict, blank=True)
    replacement = models.JSONField(default=dict, blank=True)
    edited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "region", "trimester", "section", "item_key"])]


class HospitalPreference(UUIDPrimaryKeyModel, TimeStampedModel):
    """What a hospital's staff keep correcting, once an admin has approved it as
    guidance for future prompts. Created as ``suggested`` automatically when
    enough distinct staff correct the same item; never crosses hospitals."""

    STATUS_SUGGESTED = "suggested"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_INACTIVE = "inactive"
    STATUS_CHOICES = [
        (STATUS_SUGGESTED, "Suggested"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_INACTIVE, "Inactive"),
    ]
    OPEN_STATUSES = (STATUS_SUGGESTED, STATUS_APPROVED)

    organization = models.ForeignKey("organization.Organization", on_delete=models.CASCADE, related_name="+")
    region = models.CharField(max_length=20, blank=True)
    trimester = models.PositiveSmallIntegerField(null=True, blank=True)
    section = models.CharField(max_length=20, choices=SECTION_CHOICES)
    item_key = models.CharField(max_length=80)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_SUGGESTED, db_index=True)
    # {"item_key", "guidance", "replacements": [...]} -- plain data for a prompt.
    payload = models.JSONField(default=dict)
    supporting_staff = models.PositiveSmallIntegerField(default=0)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One live row per (hospital, region, trimester, section, item): a
            # rejected or deactivated row doesn't block a future suggestion.
            models.UniqueConstraint(
                fields=["organization", "region", "trimester", "section", "item_key"],
                condition=models.Q(status__in=["suggested", "approved"]),
                name="uniq_open_hospital_preference",
            ),
        ]
