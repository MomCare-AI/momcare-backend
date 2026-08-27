"""Tenant (Organization) row scoping — the enforcement half of the B2B
multi-tenant design (architecture blueprint §2/§8).

MomCare is shared-schema multi-tenant: every hospital is one ``Organization``
row, and every tenant-owned table carries (directly or via a FK chain)
an ``organization`` column. This module is the *application-level* half of
the two-layer defense — Postgres Row-Level Security (added per-app via a
migration once each app's schema is final) is the second, independent layer.
A missed ``.filter()`` here should never be the only thing standing between
one hospital's data and another's.

``platform_admin`` and superusers are not tenant-restricted — they operate
across every hospital. Every other role belongs to exactly one Organization
(``User.organization``).
"""

from __future__ import annotations

from django.conf import settings
from django.db import transaction

from momcare_platform.core.common import rls
from momcare_platform.core.common.permissions import user_role_code


def sees_all_organizations(user) -> bool:
    """Platform admins and superusers are not tenant-restricted."""
    if not (user and user.is_authenticated):
        return False
    return user.is_superuser or user_role_code(user) == settings.ROLE_PLATFORM_ADMIN


def user_organization_id(user):
    """The single Organization id a non-platform-admin user belongs to, or None."""
    if not (user and user.is_authenticated):
        return None
    return user.organization_id


class OrganizationScopedQuerysetMixin:
    """Filter a viewset queryset to the requesting user's hospital.

    - platform_admin / superuser: unrestricted, sees every hospital.
    - Everyone else: only rows belonging to their own Organization.

    Set ``organization_lookup`` on a viewset to the ORM path from its model to
    ``organization.Organization`` (e.g. ``"organization"`` or
    ``"location__organization"``) and call ``self.scope_to_organization(queryset)``
    from ``get_queryset``.
    """

    organization_lookup = "organization"

    def dispatch(self, request, *args, **kwargs):
        """Every request handled by a scoped view runs in one transaction.

        The database-level scoping in ``rls.py`` uses ``SET LOCAL``, which only
        applies inside an open transaction and is discarded when it ends. This
        is what keeps a scoped view's queries all seeing the same, correct
        organization for the whole request, and stops the setting leaking into
        whatever request reuses this connection next.
        """
        with transaction.atomic():
            return super().dispatch(request, *args, **kwargs)

    def scope_to_organization(self, queryset):
        user = self.request.user
        if sees_all_organizations(user):
            # Not scoped at the database level either. In practice this path
            # is not expected to be reached from an ordinary hospital-scoped
            # view - a platform admin has no organization and works through
            # Django admin instead (see MyOrganizationView) - so RLS denying
            # everything here, once a non-bypassing role is in front of it, is
            # a safe failure rather than a real regression.
            return queryset
        org_id = user_organization_id(user)
        if org_id is None:
            return queryset.none()
        rls.set_current_organization(org_id)
        return queryset.filter(**{self.organization_lookup: org_id})


# ---------------------------------------------------------------------------
# Location scoping — the second, nested layer: within one hospital, a
# location-scoped role (Provider / Nurse / Care Manager) only sees records
# belonging to their assigned locations. Hospital admins see every location
# within their own hospital (still bounded by organization scoping above).
# ---------------------------------------------------------------------------

LOCATION_SCOPED_ROLE_CODES = frozenset(
    {
        settings.ROLE_PROVIDER,
        settings.ROLE_CARE_MANAGER,
        settings.ROLE_NURSE,
    },
)


def sees_all_locations_in_org(user) -> bool:
    """Hospital admins (and platform admins/superusers) aren't location-restricted."""
    if sees_all_organizations(user):
        return True
    return user_role_code(user) == settings.ROLE_HOSPITAL_ADMIN


def user_location_ids(user):
    if not (user and user.is_authenticated):
        return []
    return list(user.locations.values_list("id", flat=True))


class LocationScopedQuerysetMixin(OrganizationScopedQuerysetMixin):
    """Compose organization scoping with a second, narrower location filter."""

    location_lookup = "location"

    def scope_to_locations(self, queryset):
        queryset = self.scope_to_organization(queryset)
        user = self.request.user
        if sees_all_locations_in_org(user):
            return queryset
        if user_role_code(user) in LOCATION_SCOPED_ROLE_CODES:
            return queryset.filter(**{f"{self.location_lookup}__in": user_location_ids(user)})
        return queryset.none()
