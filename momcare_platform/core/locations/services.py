"""Location service functions."""

from __future__ import annotations

from django.db import transaction
from django.db.models.deletion import ProtectedError

from momcare_platform.core.locations.models import Location

DEFAULT_LOCATION_NAME = "Main Branch"


class LocationError(Exception):
    """Raised when a location operation cannot proceed; message is user-safe."""


def ensure_default_location(organization) -> Location:
    """Guarantee a hospital has somewhere to admit patients to.

    Patients belong to a Location, never directly to an Organization, so a
    hospital with no location cannot enrol anyone. Approval creates a default
    site rather than making the admin do it first.

    ``location_manager`` is set to the organization's own owner (the
    hospital_admin who registered it) — a location manager is compulsory on
    every location, and at this exact moment nobody else exists yet to be
    one. A hospital_admin creating further locations later must name a
    manager explicitly (see ``create_location``); this is the one place a
    default is reasonable, because there's no one else to ask.

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
            "location_manager": organization.owner,
        },
    )
    return location


def deactivate_location(location, *, by=None, reason: str = ""):
    """Deactivate a location — never delete. Blocked unless it has zero
    active patients; move them to another active location first (see
    ``move_patients_to_location``).

    Unlike Organization deactivation, this guard is real and necessary: a
    Location is a *middle* node in the tenancy hierarchy — there's always
    another active location within the same hospital for its patients to go
    to — whereas Organization sits at the top, with nowhere to send anyone.
    """
    active = location.active_patient_count
    if active:
        msg = (
            f"This location can't be deactivated because {active} active patient(s) "
            "are currently assigned to it. Move those patients to another location first."
        )
        raise LocationError(msg)
    location.deactivate(by=by, reason=reason)
    return location


def reactivate_location(location):
    """Restore a previously deactivated location."""
    location.reactivate()
    return location


def delete_location(location) -> None:
    """Hard-delete — mirrors Neuro_RPM's own guard: only a location that has
    already been deactivated may be permanently removed. Deactivation stays
    the normal path everywhere else in this system (never delete); this
    exists for the case a location was created by mistake and never should
    have existed at all.

    ``Patient.location`` is ``on_delete=PROTECT`` — Postgres itself refuses
    the delete if *any* patient, active or not, was ever assigned here
    (patients are never hard-deleted either, so that reference outlives the
    patient's own is_active flag). That refusal is caught and turned into
    the same clean, user-safe error as everything else in this module,
    instead of a raw 500.
    """
    if location.is_active:
        raise LocationError("This location must be deactivated before it can be deleted.")
    try:
        location.delete()
    except ProtectedError as exc:
        msg = (
            "This location cannot be deleted because it has patient history — "
            "hard delete only works for a location that never had any patients."
        )
        raise LocationError(msg) from exc


@transaction.atomic
def move_patients_to_location(source, target, *, move_all: bool, patient_ids=None):
    """Bulk-move active patients from ``source`` to ``target`` — the
    necessary companion to ``deactivate_location``'s guard.

    Two modes: ``move_all=True`` moves every active patient at ``source``
    (``patient_ids`` ignored even if supplied); ``move_all=False`` moves only
    the given ``patient_ids``, failing the whole request — no partial move —
    if any of them isn't actually an active patient of ``source``.

    Returns the number moved.
    """
    from momcare_platform.core.patients.models import Patient  # noqa: PLC0415

    if target.pk == source.pk:
        raise LocationError("Target must be a different location.")
    if not target.is_active:
        raise LocationError("Target location is not active.")

    if move_all:
        return Patient.objects.filter(location=source, is_active=True).update(location=target)

    requested_ids = [str(pid) for pid in (patient_ids or [])]
    if not requested_ids:
        raise LocationError("patient_ids is required and cannot be empty when move_all is false.")

    found_ids = set(
        Patient.objects.filter(pk__in=requested_ids, location=source, is_active=True).values_list("pk", flat=True),
    )
    missing = sorted(set(requested_ids) - {str(pk) for pk in found_ids})
    if missing:
        raise LocationError(f"These patient IDs are not active patients of this location: {missing}")

    return Patient.objects.filter(pk__in=requested_ids, location=source, is_active=True).update(location=target)


def create_location(*, organization, name: str, location_manager, **fields) -> Location:
    """Create an additional location for a hospital that already exists.

    ``location_manager`` is a required parameter, not an optional field with
    a default — a location manager is compulsory on every location. The one
    exception, an org's very first "Main Branch", goes through
    ``ensure_default_location`` instead, which has an actual person (the
    owner) to fall back to; this function has no such fallback to offer.
    """
    return Location.objects.create(
        organization=organization,
        name=name,
        location_manager=location_manager,
        **fields,
    )
