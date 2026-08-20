from django.conf import settings
from django.db import models

from momcare_platform.core.common.models import Deactivatable, TimeStampedModel, UUIDPrimaryKeyModel


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
