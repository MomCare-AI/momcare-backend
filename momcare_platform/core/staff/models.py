import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from momcare_platform.core.common.models import Deactivatable, TimeStampedModel, UUIDPrimaryKeyModel

INVITE_TTL_DAYS = 14


def generate_invite_token() -> str:
    """URL-safe, unguessable token. The invite link is the only credential the
    recipient holds until they set a password, so it must not be enumerable."""
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

    class Meta:
        ordering = ["employee_id"]

    def __str__(self) -> str:
        return f"{self.user.get_full_name()} ({self.employee_id})"

    @property
    def current_patient_count(self) -> int:
        # Computed at runtime, never stored (blueprint §9) — real filter added
        # once the patient-assignment relationship exists.
        return 0

    @property
    def has_capacity(self) -> bool:
        if self.max_patients is None:
            return True
        return self.current_patient_count < self.max_patients


class StaffInvite(UUIDPrimaryKeyModel, TimeStampedModel):
    """A pending invitation for someone to join one hospital in a given role.

    Clinical staff never self-register: tenant membership is granted from inside
    the tenant, by a hospital admin who is accountable for the people they
    vouch for. This model is that grant, held until the recipient accepts.

    The organization and role are fixed at invite time and are never taken from
    the acceptance request — otherwise a recipient could edit their own way into
    a different hospital or a higher role.
    """

    organization = models.ForeignKey(
        "organization.Organization",
        on_delete=models.CASCADE,
        related_name="staff_invites",
    )
    email = models.EmailField()
    first_name = models.CharField(max_length=50, blank=True)
    last_name = models.CharField(max_length=50, blank=True)
    role = models.ForeignKey("users.Role", on_delete=models.PROTECT, related_name="+")
    token = models.CharField(max_length=64, unique=True, default=generate_invite_token, db_index=True)
    expires_at = models.DateTimeField()

    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sent_staff_invites",
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="accepted_invite",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "email"])]
        constraints = [
            # One live invite per address per hospital. Accepted and revoked
            # invites are kept for the audit trail, so they're excluded here.
            models.UniqueConstraint(
                fields=["organization", "email"],
                condition=models.Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name="unique_pending_invite_per_org_email",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.email} → {self.organization.name} ({self.role.name})"

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(days=INVITE_TTL_DAYS)
        super().save(*args, **kwargs)

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_pending(self) -> bool:
        return self.accepted_at is None and self.revoked_at is None and not self.is_expired

    @property
    def status(self) -> str:
        if self.accepted_at:
            return "accepted"
        if self.revoked_at:
            return "revoked"
        if self.is_expired:
            return "expired"
        return "pending"
