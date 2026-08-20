"""Role-based authorization via DRF permission classes.

Authorization is expressed as explicit, reusable permission classes — not Django
Groups or the model-permission system. This keeps the rules visible and aligned
with the API-first architecture.

A user's role comes from ``User.role`` (FK to ``users.Role``), matched by its
stable ``code``. Superusers bypass role checks.
"""

from __future__ import annotations

from django.conf import settings
from rest_framework.permissions import BasePermission


def user_role_code(user) -> str | None:
    """Return the user's role code, or None."""
    role = getattr(user, "role", None)
    return getattr(role, "code", None)


class HasRole(BasePermission):
    """Allow only authenticated users whose role code is in ``allowed_roles``.

    Superusers bypass the check. Subclass and set ``allowed_roles``.
    """

    allowed_roles: frozenset[str] = frozenset()

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        return user_role_code(user) in self.allowed_roles


class IsPlatformAdmin(HasRole):
    """Momcare's own staff — sees across every hospital."""

    allowed_roles = frozenset({settings.ROLE_PLATFORM_ADMIN})


class IsHospitalAdmin(HasRole):
    allowed_roles = frozenset({settings.ROLE_HOSPITAL_ADMIN})


class IsProvider(HasRole):
    allowed_roles = frozenset({settings.ROLE_PROVIDER})


class IsCareManager(HasRole):
    allowed_roles = frozenset({settings.ROLE_CARE_MANAGER})


class IsNurse(HasRole):
    allowed_roles = frozenset({settings.ROLE_NURSE})


class IsPatient(HasRole):
    allowed_roles = frozenset({settings.ROLE_PATIENT})


class IsHospitalStaff(HasRole):
    """Any hospital-side staff role (admin/provider/care_manager/nurse) — excludes patients."""

    allowed_roles = frozenset(
        {
            settings.ROLE_HOSPITAL_ADMIN,
            settings.ROLE_PROVIDER,
            settings.ROLE_CARE_MANAGER,
            settings.ROLE_NURSE,
        },
    )
