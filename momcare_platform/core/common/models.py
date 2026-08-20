"""Shared abstract models.

- ``UUIDPrimaryKeyModel`` — UUID primary key by default for every first-party model.
- ``TimeStampedModel`` — created/updated timestamps.
- ``AddressMixin`` — structured (queryable) postal address columns.
- ``Deactivatable`` — soft-deactivation (records are never physically deleted).
"""

import uuid

from django.db import models


class UUIDPrimaryKeyModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AddressMixin(models.Model):
    """Structured address columns (preferred over JSON so they can be queried,
    validated, and used for downstream automation)."""

    address_line1 = models.CharField(max_length=255, blank=True)
    address_line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=120, blank=True)
    state = models.CharField(max_length=120, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=100, blank=True)

    class Meta:
        abstract = True


class Deactivatable(models.Model):
    """Soft-deactivation. Physical deletes are disallowed for these records;
    deactivation is auditable (when / by whom / why)."""

    is_active = models.BooleanField(default=True, db_index=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deactivated_by = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    deactivation_reason = models.TextField(blank=True)

    class Meta:
        abstract = True

    def deactivate(self, *, by=None, reason: str = "") -> None:
        from django.utils import timezone

        self.is_active = False
        self.deactivated_at = timezone.now()
        self.deactivated_by = by
        self.deactivation_reason = reason
        self.save(
            update_fields=[
                "is_active",
                "deactivated_at",
                "deactivated_by",
                "deactivation_reason",
                "updated_at",
            ],
        )

    def reactivate(self) -> None:
        self.is_active = True
        self.deactivated_at = None
        self.deactivated_by = None
        self.deactivation_reason = ""
        self.save(
            update_fields=[
                "is_active",
                "deactivated_at",
                "deactivated_by",
                "deactivation_reason",
                "updated_at",
            ],
        )
