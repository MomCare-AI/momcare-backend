from django.conf import settings
from django.db import models
from timezone_field import TimeZoneField

from momcare_platform.core.common.models import AddressMixin, Deactivatable, TimeStampedModel, UUIDPrimaryKeyModel
from momcare_platform.core.organization.models import DATE_FORMAT_CHOICES


class Location(UUIDPrimaryKeyModel, AddressMixin, Deactivatable, TimeStampedModel):
    """A physical site (clinic/branch) belonging to one hospital (Organization).

    Patients belong to a Location, never directly to the Organization — same
    hierarchy as Neuro_RPM (Organization -> Location -> Patient), except here
    Organization is one of many hospitals, not a singleton. Sites are never
    physically deleted, only deactivated.
    """

    organization = models.ForeignKey(
        "organization.Organization",
        on_delete=models.PROTECT,
        related_name="locations",
    )
    name = models.CharField(max_length=120)
    timezone = TimeZoneField(default="UTC")
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    location_manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managed_locations",
        help_text="The staff member responsible for managing this location.",
    )
    date_format = models.CharField(
        max_length=10,
        choices=DATE_FORMAT_CHOICES,
        blank=True,
        default="",
        help_text="Override organization date format for this location. Leave blank to inherit.",
    )

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_location_name_per_org"),
        ]
        indexes = [models.Index(fields=["organization", "is_active"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.organization.name})"

    @property
    def active_patient_count(self) -> int:
        return self.patients.filter(is_active=True).count()
