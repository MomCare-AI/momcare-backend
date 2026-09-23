"""Clinical contact logging — a record that staff spoke with or about a
patient, separate from the machine-recorded vitals in
``modules.pregnancy.vitals``.

Adapted from Neuro_RPM's own ``core.monitoring`` (ClinicalTag/
MonitoringSession/MonitoringNote), with three deliberate departures from
that reference implementation:

1. **No RPM/CCM program split.** Neuro_RPM's ``MonitoringSession`` carries
   separate ``rpm_duration_seconds``/``ccm_duration_seconds``/
   ``oor_duration_seconds`` fields because a single call can count toward
   two concurrently-open CMS billing programs at once. MomCare has exactly
   one program (pregnancy monitoring) and bills no insurer, so that split
   collapses to one ``duration_seconds`` field. ``note_type`` (which
   program a note counted toward) is dropped for the same reason.
2. **Attaches to Patient, with an optional Pregnancy.** Neuro_RPM attaches
   to ``Patient`` directly -- not even to its own episode table,
   ``PatientProgramEnrollment`` -- for the same multi-program reason above.
   MomCare's Pregnancy *is* clinically meaningful the way Neuro_RPM's
   enrollment isn't (a symptom means something different at 12 weeks vs. 38),
   and a patient has at most one active pregnancy at a time (see
   ``Pregnancy``'s own ``one_active_pregnancy_per_patient`` constraint), so
   there's no multi-program ambiguity to avoid by skipping it. But
   ``onboard_patient()`` allows a patient with no pregnancy at all
   (``pregnancy_data`` is optional), so ``pregnancy`` here is nullable and
   auto-filled from ``patient.current_pregnancy`` at creation time
   (``services.create_combined_monitoring``) rather than required.
3. **ClinicalTag is tenant-scoped.** Neuro_RPM is single-tenant, so its
   ``ClinicalTag`` is one global, unscoped list. MomCare is shared-schema
   multi-tenant, so a tag needs an owner -- following the same
   organization-or-location hierarchy Neuro_RPM itself uses for
   ``NoteTemplate``/``ChronicCondition``/``Medication`` (org-level entries
   visible hospital-wide; location-level entries visible only there; a new
   Location copies its organization's tags down as independent rows --
   see ``signals.py``).
"""

from __future__ import annotations

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone

from momcare_platform.core.common.models import TimeStampedModel, UUIDPrimaryKeyModel

HEX_COLOR_VALIDATOR = RegexValidator(
    regex=r"^#[0-9A-Fa-f]{6}$",
    message="color must be a hex code like #RRGGBB.",
)

# A monitoring contact can't sensibly run longer than a day, nor claim zero
# duration -- a zero-length session is a note without a session (see
# services.create_combined_monitoring).
MAX_SESSION_DURATION_SECONDS = 86400


class ClinicalTag(UUIDPrimaryKeyModel, TimeStampedModel):
    """A reusable clinical label attached to monitoring notes.

    Scoped to exactly one of ``organization`` or ``location`` -- never both,
    never neither (enforced below). Org-level tags are visible to every
    location of that hospital; a location's own tags are visible only
    there. Populated via get-or-create (``services.get_or_create_tags``) so
    the dropdown grows organically as staff type new tags, never created
    directly through a client-supplied id. An existing tag's ``color`` is
    never silently overwritten by a later get-or-create call -- only the
    dedicated edit endpoint changes it.
    """

    organization = models.ForeignKey(
        "organization.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="clinical_tags",
    )
    location = models.ForeignKey(
        "locations.Location",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="clinical_tags",
    )
    name = models.CharField(max_length=100)
    # null=True (not just blank) distinguishes "no color ever set" (None)
    # from a future empty-string convention -- same reasoning as Neuro_RPM's
    # own field.
    color = models.CharField(max_length=7, null=True, blank=True, validators=[HEX_COLOR_VALIDATOR])  # noqa: DJ001

    class Meta:
        ordering = ["name"]
        verbose_name = "Clinical Tag"
        verbose_name_plural = "Clinical Tags"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(organization__isnull=False, location__isnull=True)
                    | models.Q(organization__isnull=True, location__isnull=False)
                ),
                name="clinicaltag_exactly_one_scope",
            ),
            # Case-sensitive DB backstop; case-insensitive matching itself
            # happens in get_or_create_tags, same split Neuro_RPM uses.
            models.UniqueConstraint(
                fields=["organization", "name"],
                condition=models.Q(organization__isnull=False),
                name="unique_org_clinical_tag_name",
            ),
            models.UniqueConstraint(
                fields=["location", "name"],
                condition=models.Q(location__isnull=False),
                name="unique_location_clinical_tag_name",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "name"]),
            models.Index(fields=["location", "name"]),
        ]

    def __str__(self) -> str:
        return self.name


class MonitoringSession(UUIDPrimaryKeyModel, TimeStampedModel):
    """A single, independent block of time spent on a patient contact --
    a call, a chart review, a follow-up. Every session is its own row
    (never merged/accumulated server-side). Hard-deleted -- no soft delete.
    """

    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.CASCADE,
        related_name="monitoring_sessions",
    )
    # Nullable: onboard_patient() allows a Patient with no Pregnancy yet, and
    # a note logged between two pregnancies has nowhere else to point. Set
    # automatically from patient.current_pregnancy when one exists --
    # see services.create_combined_monitoring.
    pregnancy = models.ForeignKey(
        "patients.Pregnancy",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="monitoring_sessions",
    )
    duration_seconds = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(MAX_SESSION_DURATION_SECONDS)],
    )
    # Actual time of the clinical contact (may be backdated for retroactive
    # entries) -- distinct from created_at/updated_at, which are system-managed.
    recorded_at = models.DateTimeField(default=timezone.now)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="monitoring_sessions_added",
    )

    class Meta:
        ordering = ["-recorded_at"]
        verbose_name = "Monitoring Session"
        verbose_name_plural = "Monitoring Sessions"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(duration_seconds__gt=0)
                & models.Q(duration_seconds__lte=MAX_SESSION_DURATION_SECONDS),
                name="monitoringsession_duration_range",
            ),
        ]
        indexes = [
            models.Index(fields=["patient", "recorded_at"]),
            models.Index(fields=["pregnancy", "recorded_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.patient.full_name} · {self.duration_seconds}s"


class MonitoringNote(UUIDPrimaryKeyModel, TimeStampedModel):
    """A clinical note, created only inside
    ``services.create_combined_monitoring``. Usually attached to a
    ``MonitoringSession``, but ``session`` is nullable: a note submitted
    with no duration creates no session and stands alone. ``patient`` (and
    ``pregnancy``, when known) are denormalized from the session for fast,
    join-free reads when listing/editing notes directly. Hard-deleted;
    deleting the parent session (if any) cascades to remove the note too.
    """

    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.CASCADE,
        related_name="monitoring_notes",
    )
    pregnancy = models.ForeignKey(
        "patients.Pregnancy",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="monitoring_notes",
    )
    session = models.OneToOneField(
        MonitoringSession,
        on_delete=models.CASCADE,
        related_name="note",
        null=True,
        blank=True,
    )
    note = models.TextField()
    recorded_at = models.DateTimeField(default=timezone.now)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="monitoring_notes_added",
    )
    tags = models.ManyToManyField(ClinicalTag, blank=True, related_name="notes")

    # Outcome of a call attempt this note logs. At most one may be true -- a
    # single call either reached the patient (two-way) or didn't and a
    # voicemail was left instead. Both false is valid (e.g. no call involved).
    left_voicemail = models.BooleanField(default=False)
    two_way_communication = models.BooleanField(default=False)

    class Meta:
        ordering = ["-recorded_at"]
        verbose_name = "Monitoring Note"
        verbose_name_plural = "Monitoring Notes"
        indexes = [
            models.Index(fields=["patient", "recorded_at"]),
            models.Index(fields=["pregnancy", "recorded_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(left_voicemail=True, two_way_communication=True),
                name="monitoringnote_not_both_call_outcomes",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.patient.full_name} · {self.recorded_at:%Y-%m-%d}"
