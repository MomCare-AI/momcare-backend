from django.conf import settings
from django.db import models

from momcare_platform.core.common.models import Deactivatable, TimeStampedModel, UUIDPrimaryKeyModel


class Patient(UUIDPrimaryKeyModel, Deactivatable, TimeStampedModel):
    """The bare patient entity — deliberately no medical/maternal-health
    fields (pregnancy, vitals, care plans). Those belong to the first feature
    module's own design (blueprint §18 scope split), built on top of this
    once it exists.

    Belongs to a Location (never directly to the Organization), same
    Organization -> Location -> Patient hierarchy as Neuro_RPM.
    """

    location = models.ForeignKey(
        "locations.Location",
        on_delete=models.PROTECT,
        related_name="patients",
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="patient_profile",
    )
    # Medical record number — an identifier, not clinical content. NULL is
    # exempt from Postgres uniqueness, so real ties can exist here — this is
    # exactly the case StableOrderingFilter (common/pagination.py) exists for.
    mrn = models.CharField(max_length=100, unique=True, null=True, blank=True)
    consent_date = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["mrn"])]

    def __str__(self) -> str:
        return self.user.get_full_name() or self.user.email

    @property
    def first_name(self) -> str:
        return self.user.first_name

    @property
    def last_name(self) -> str:
        return self.user.last_name

    @property
    def full_name(self) -> str:
        return self.user.get_full_name()

    @property
    def date_of_birth(self):
        return self.user.date_of_birth

    @property
    def gender(self) -> str:
        return self.user.gender

    @property
    def email(self) -> str:
        return self.user.email
