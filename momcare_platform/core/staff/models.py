import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

from momcare_platform.core.common.models import Deactivatable, TimeStampedModel, UUIDPrimaryKeyModel
from momcare_platform.core.common.storage import DatabaseStorage


def generate_invite_token() -> str:
    """Dead code, kept only so migration 0003_add_staff_invite (already
    applied everywhere, including production) can still be replayed from
    scratch — its CreateModel operation references this dotted path as a
    field default. The StaffInvite model itself is gone; see
    0006_remove_staff_invite. Do not call this or reuse it for anything new.
    """
    return secrets.token_urlsafe(32)


class Staff(UUIDPrimaryKeyModel, Deactivatable, TimeStampedModel):
    """Employment record for a hospital-side User (hospital_admin/provider/
    nurse/care_manager). Deliberately separate from User — User is login
    credentials, Staff is "what this person does at this hospital."

    ``max_patients``/``current_patient_count`` back the hard-capacity check in
    the assignment service (blueprint §9's concurrency-safe pattern) — kept
    minimal here; the actual assignment service is future feature work.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="staff",
    )
    employee_id = models.CharField(max_length=20, unique=True)
    max_patients = models.PositiveIntegerField(null=True, blank=True)
    current_location = models.ForeignKey(
        "locations.Location",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    # Credentialing — self-reported, not verified against any registry. The
    # directory shows what the person entered, not claimed to be more than it is.
    photo = models.FileField(upload_to="staff/%Y/%m/", storage=DatabaseStorage(), blank=True, null=True)
    qualifications = models.CharField(
        max_length=255,
        blank=True,
        help_text="e.g. MBBS, FCPS (Gynae & Obs)",
    )
    specialty = models.CharField(max_length=150, blank=True)
    registration_number = models.CharField(max_length=100, blank=True)
    registration_authority = models.CharField(
        max_length=150,
        blank=True,
        help_text="e.g. PMDC, Pakistan Nursing Council",
    )
    # Experience is derived from this on every read, never stored as a raw
    # number of years - the same reasoning as gestational age elsewhere in
    # this codebase: a stored "12 years" is wrong the following year.
    practicing_since = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["employee_id"]

    def __str__(self) -> str:
        return f"{self.user.get_full_name()} ({self.employee_id})"

    @property
    def current_patient_count(self) -> int:
        """Active caseload: patients whose pregnancy this staff member is the
        ``provider``, ``nurse`` or ``care_manager`` for — restricted to
        pregnancies still ``STATUS_ACTIVE`` and patients still active.

        A set union, not a sum: one staff member holding two slots on the
        same pregnancy is still one patient of caseload, not two.

        Computed at runtime, never stored (blueprint §9).
        """
        from momcare_platform.core.patients.models import Pregnancy  # noqa: PLC0415

        active = {"status": Pregnancy.STATUS_ACTIVE, "patient__is_active": True}
        provider_ids = self.assigned_pregnancies.filter(**active).values_list("patient_id", flat=True)
        nurse_ids = self.nursed_pregnancies.filter(**active).values_list("patient_id", flat=True)
        care_manager_ids = self.care_managed_pregnancies.filter(**active).values_list("patient_id", flat=True)
        return len(set(provider_ids) | set(nurse_ids) | set(care_manager_ids))

    @property
    def has_capacity(self) -> bool:
        if self.max_patients is None:
            return True
        return self.current_patient_count < self.max_patients

    @property
    def years_of_experience(self) -> int | None:
        if self.practicing_since is None:
            return None
        today = timezone.now().date()
        years = today.year - self.practicing_since.year
        if (today.month, today.day) < (self.practicing_since.month, self.practicing_since.day):
            years -= 1
        return max(years, 0)


class SecondaryProvider(UUIDPrimaryKeyModel, TimeStampedModel):
    """An external clinician referenced on a patient's record — not platform
    staff, no login, no role, no permissions.

    The referring doctor at the rural clinic who sent her here and stays
    involved, or the specialist she also sees elsewhere. Deliberately a table
    of its own rather than columns on Patient, because one such clinician is
    shared across many patients: update their phone number once, not per
    patient. That is exactly the opposite of ``Patient.emergency_contact_*``,
    which is her own husband or mother and belongs to her row alone.

    Unlike the reference implementation this carries an ``organization`` FK.
    Neuro_RPM is single-tenant so its equivalent needs none; MomCare is
    shared-schema multi-tenant, and without this column every hospital would
    read every other hospital's referral list.
    """

    organization = models.ForeignKey(
        "organization.Organization",
        on_delete=models.PROTECT,
        related_name="secondary_providers",
    )
    name = models.CharField(max_length=150)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    # Free text rather than a choices list: "MBBS, Rural Health Centre Kahuta"
    # is the kind of thing staff actually write, and a fixed vocabulary for
    # clinicians outside this platform would be guesswork.
    affiliation = models.CharField(
        max_length=200,
        blank=True,
        help_text="Where they practise — e.g. 'Rural Health Centre, Kahuta'.",
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "Secondary Provider"
        verbose_name_plural = "Secondary Providers"
        indexes = [models.Index(fields=["organization", "name"])]

    def __str__(self) -> str:
        return self.name
