"""Location service functions."""

from __future__ import annotations

from momcare_platform.core.locations.models import Location

DEFAULT_LOCATION_NAME = "Main Branch"


def ensure_default_location(organization) -> Location:
    """Guarantee a hospital has somewhere to admit patients to.

    Patients belong to a Location, never directly to an Organization, so a
    hospital with no location cannot enrol anyone. Approval creates a default
    site rather than making the admin do it first.

    Idempotent, and deliberately conservative: a hospital that already has any
    active location is left alone, so this never adds a redundant "Main Branch"
    beside real sites someone has already set up.
    """
    existing = organization.locations.filter(is_active=True).order_by("created_at").first()
    if existing is not None:
        return existing

    location, _created = Location.objects.get_or_create(
        organization=organization,
        name=DEFAULT_LOCATION_NAME,
        defaults={
            "timezone": organization.timezone,
            "phone": organization.phone,
            "email": organization.email,
            "address_line1": organization.address_line1,
            "address_line2": organization.address_line2,
            "city": organization.city,
            "state": organization.state,
            "postal_code": organization.postal_code,
            "country": organization.country,
        },
    )
    return location
