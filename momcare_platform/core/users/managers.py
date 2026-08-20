from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.models import BaseUserManager
from django.db import models

if TYPE_CHECKING:
    from collections.abc import Iterable


PLATFORM_ADMIN_ROLE_CODE = "platform_admin"


class UserQuerySet(models.QuerySet["User"]):
    """Chainable, reusable query scopes shared across apps."""

    def active(self) -> UserQuerySet:
        return self.filter(is_active=True)

    def with_role(self, *codes: str) -> UserQuerySet:
        return self.filter(role__code__in=codes)

    def for_locations(self, location_ids: Iterable) -> UserQuerySet:
        return self.filter(locations__in=location_ids).distinct()


class UserManager(BaseUserManager.from_queryset(UserQuerySet)):  # type: ignore[misc]
    """Email-keyed manager for the custom User model."""

    use_in_migrations = True

    def _create_user(
        self,
        email: str,
        password: str | None = None,
        *,
        username: str | None = None,
        **extra_fields,
    ):
        if not email:
            msg = "Users must have an email address."
            raise ValueError(msg)
        email = self.normalize_email(email).lower()
        if username:
            username = username.lower()
        user = self.model(email=email, username=username, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra_fields):  # type: ignore[override]
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email: str, password: str | None = None, **extra_fields):  # type: ignore[override]
        # A superuser bootstraps a Momcare platform operator, not a hospital
        # admin — it isn't scoped to any single Organization (see User.organization).
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            msg = "Superuser must have is_staff=True."
            raise ValueError(msg)
        if extra_fields.get("is_superuser") is not True:
            msg = "Superuser must have is_superuser=True."
            raise ValueError(msg)

        if "role" not in extra_fields:
            from .models import Role  # noqa: PLC0415 — avoid import cycle at module load

            extra_fields["role"] = Role.objects.filter(code=PLATFORM_ADMIN_ROLE_CODE).first()

        return self._create_user(email, password, **extra_fields)
