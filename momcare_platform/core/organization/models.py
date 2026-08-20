from django.conf import settings
from django.db import models
from timezone_field import TimeZoneField

from momcare_platform.core.common.models import AddressMixin, Deactivatable, TimeStampedModel, UUIDPrimaryKeyModel

DATE_FORMAT_CHOICES = [
    ("DD-MM-YYYY", "DD-MM-YYYY"),
    ("MM-DD-YYYY", "MM-DD-YYYY"),
    ("YYYY-MM-DD", "YYYY-MM-DD"),
]


class Organization(UUIDPrimaryKeyModel, AddressMixin, Deactivatable, TimeStampedModel):
    """One hospital (tenant). Unlike Neuro_RPM's singleton Organization, MomCare
    is a B2B multi-tenant platform (blueprint §2) — many hospitals share this
    database, each as its own Organization row. Every tenant-owned model
    carries an ``organization`` FK (directly or via ``Location``), enforced by
    the scoping mixin in ``core.common.scoping`` and, per-app, Postgres RLS.
    """

    name = models.CharField(max_length=255)
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

        return Patient.objects.filter(is_active=True, user__organization=self).count()


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
