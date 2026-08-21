from django.conf import settings
from django.db import models
from timezone_field import TimeZoneField

from momcare_platform.core.common.models import AddressMixin, Deactivatable, TimeStampedModel, UUIDPrimaryKeyModel

DATE_FORMAT_CHOICES = [
    ("DD-MM-YYYY", "DD-MM-YYYY"),
    ("MM-DD-YYYY", "MM-DD-YYYY"),
    ("YYYY-MM-DD", "YYYY-MM-DD"),
]

# Facilities in Pakistan are licensed provincially, not federally (PMDC registers
# individual practitioners, not establishments). Recording the issuing body tells
# the reviewer which public register to search for a given licence number.
LICENSE_AUTHORITY_CHOICES = [
    ("phc", "Punjab Healthcare Commission (PHC)"),
    ("shcc", "Sindh Healthcare Commission (SHCC)"),
    ("kphcc", "KP Healthcare Commission"),
    ("bhcc", "Balochistan Healthcare Commission"),
    ("ihra", "Islamabad Healthcare Regulatory Authority (IHRA)"),
    ("ajk_gb", "AJK / Gilgit-Baltistan health department"),
    ("other", "Other / not listed"),
]


class Organization(UUIDPrimaryKeyModel, AddressMixin, Deactivatable, TimeStampedModel):
    """One hospital (tenant). Unlike Neuro_RPM's singleton Organization, MomCare
    is a B2B multi-tenant platform (blueprint §2) — many hospitals share this
    database, each as its own Organization row. Every tenant-owned model
    carries an ``organization`` FK (directly or via ``Location``), enforced by
    the scoping mixin in ``core.common.scoping`` and, per-app, Postgres RLS.

    ``status`` gates tenant access: self-registration creates a PENDING row and
    its members cannot authenticate until a platform admin approves it. This is
    separate from ``is_active`` (soft-deactivation) — a hospital can be rejected
    without ever having been active, or suspended long after approval.
    """

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_SUSPENDED = "suspended"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending review"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_SUSPENDED, "Suspended"),
    ]

    name = models.CharField(max_length=255)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    review_note = models.TextField(
        blank=True,
        help_text="What the reviewer actually checked — registry consulted, date, callback outcome.",
    )
    license_no = models.CharField(max_length=100, blank=True)
    license_authority = models.CharField(
        max_length=20,
        choices=LICENSE_AUTHORITY_CHOICES,
        blank=True,
        help_text="Which regulator issued the licence — tells the reviewer whose register to search.",
    )
    license_document = models.FileField(
        upload_to="licenses/%Y/%m/",
        blank=True,
        null=True,
        help_text="Scan of the licence certificate, for the reviewer to inspect.",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_organizations",
        null=True,
        blank=True,
    )
    logo = models.URLField(max_length=500, blank=True)
    timezone = TimeZoneField(default="UTC")
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    established_date = models.DateField(null=True, blank=True)
    date_format = models.CharField(max_length=10, choices=DATE_FORMAT_CHOICES, default="MM-DD-YYYY")

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def can_authenticate(self) -> bool:
        """Members may sign in only once the hospital is approved and still active."""
        return self.status == self.STATUS_APPROVED and self.is_active

    def set_review_status(self, status: str, *, by=None, note: str = "", notify: bool = True) -> None:
        """Record a platform-admin review decision, with who decided and when.

        Notifies the owner by default: an applicant who is told to wait for a
        decision has no other way of learning one was made. Pass
        ``notify=False`` for data migrations and backfills.
        """
        from django.utils import timezone  # noqa: PLC0415

        from momcare_platform.core.common.mail import (  # noqa: PLC0415
            send_application_approved,
            send_application_rejected,
        )

        previous = self.status
        self.status = status
        self.reviewed_at = timezone.now()
        self.reviewed_by = by
        if note:
            self.review_note = note
        self.save(
            update_fields=["status", "reviewed_at", "reviewed_by", "review_note", "updated_at"],
        )

        # An approved hospital needs somewhere to admit patients to: Patient
        # requires a Location, so without this the first enrolment would fail.
        if status == self.STATUS_APPROVED:
            from momcare_platform.core.locations.services import ensure_default_location  # noqa: PLC0415

            ensure_default_location(self)

        # Only on a genuine change, so re-running an action doesn't re-notify.
        if not (notify and self.owner and previous != status):
            return
        if status == self.STATUS_APPROVED:
            send_application_approved(self.owner, self)
        elif status == self.STATUS_REJECTED:
            send_application_rejected(self.owner, self, note=self.review_note)

    # ── Computed counts (no denormalization — always fresh) ───────────────────
    @property
    def location_count(self) -> int:
        return self.locations.filter(is_active=True).count()

    @property
    def staff_count(self) -> int:
        from momcare_platform.core.staff.models import Staff  # noqa: PLC0415

        return Staff.objects.filter(is_active=True, user__organization=self).distinct().count()

    @property
    def patient_count(self) -> int:
        from momcare_platform.core.patients.models import Patient  # noqa: PLC0415

        # Counted through Location, not User: a patient need not have an app
        # account, and counting via ``user__organization`` silently reported
        # zero for every patient enrolled without one.
        return Patient.objects.filter(is_active=True, location__organization=self).count()


class ModuleRegistry(UUIDPrimaryKeyModel):
    """Which feature modules THIS hospital has activated.

    Per-organization, not global — module activation is a per-hospital
    subscription concept in a multi-tenant platform (see gating.py). No
    feature modules exist yet, so this table has no rows until the first
    module is registered and a hospital activates it.
    """

    organization = models.ForeignKey(
        "organization.Organization",
        on_delete=models.CASCADE,
        related_name="module_registrations",
    )
    module_key = models.CharField(max_length=50)
    display_name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=False)
    activated_on = models.DateTimeField(null=True, blank=True)
    config = models.JSONField(default=dict)

    class Meta:
        verbose_name = "Module"
        verbose_name_plural = "Modules"
        ordering = ["organization_id", "module_key"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "module_key"], name="unique_module_per_organization"),
        ]

    def __str__(self) -> str:
        status = "active" if self.is_active else "inactive"
        return f"{self.display_name} ({self.organization_id}, {status})"

    @classmethod
    def is_module_active(cls, module_key: str, organization_id) -> bool:
        return cls.objects.filter(module_key=module_key, organization_id=organization_id, is_active=True).exists()


class AuditLog(UUIDPrimaryKeyModel):
    """HIPAA-required access log — see core.common.middleware.AuditLogMiddleware."""

    ACTION_READ = "READ"
    ACTION_CREATE = "CREATE"
    ACTION_UPDATE = "UPDATE"
    ACTION_DELETE = "DELETE"
    ACTION_CHOICES = [
        (ACTION_READ, "Read"),
        (ACTION_CREATE, "Create"),
        (ACTION_UPDATE, "Update"),
        (ACTION_DELETE, "Delete"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    resource = models.CharField(max_length=100)
    resource_id = models.CharField(max_length=100)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    endpoint = models.CharField(max_length=255, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        default_permissions = ("add", "view")
        ordering = ["-timestamp"]
        indexes = [models.Index(fields=["resource", "resource_id"])]

    def __str__(self) -> str:
        return f"{self.action} {self.resource}/{self.resource_id} at {self.timestamp}"
