"""Staff onboarding & lifecycle services."""

from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.db.models.deletion import ProtectedError

from momcare_platform.core.staff.models import Staff
from momcare_platform.core.users.models import Role, User


class StaffError(Exception):
    """Raised when a staff-lifecycle operation cannot proceed; message is
    user-safe, same convention as ``locations.services.LocationError``."""


# Roles a hospital admin (or a location's own manager, for their own location)
# may onboard directly. Deliberately excludes platform_admin (Momcare's own
# staff, created by createsuperuser) and patient (enrolled by clinical staff,
# never onboarded onto the hospital's own team).
STAFF_ROLE_CODES = frozenset(
    {
        settings.ROLE_HOSPITAL_ADMIN,
        settings.ROLE_PROVIDER,
        settings.ROLE_NURSE,
        settings.ROLE_CARE_MANAGER,
    },
)


def _next_employee_id(organization) -> str:
    """Employee ids are unique platform-wide, so scope the readable part to the
    hospital and let the count drive the sequence."""
    seq = Staff.objects.filter(user__organization=organization).count() + 1
    prefix = "".join(ch for ch in organization.name.upper() if ch.isalnum())[:4] or "ORG"
    candidate = f"{prefix}-{seq:04d}"
    while Staff.objects.filter(employee_id=candidate).exists():
        seq += 1
        candidate = f"{prefix}-{seq:04d}"
    return candidate


@transaction.atomic
def onboard_staff(
    *,
    organization,
    email: str,
    first_name: str,
    last_name: str,
    role_code: str,
    phone: str = "",
    locations=(),
) -> Staff:
    """Create a hospital staff member's account, ready to be activated.

    The account is created **passwordless** — ``create_user(password=None)``
    stores an unusable password, so it cannot be signed into at all until the
    person follows the invitation link and chooses one. ``requires_password_
    reset`` marks it as not-yet-activated for the staff list and for
    ``/api/auth/me/``.

    Nobody but the staff member ever knows their password, which is what
    makes the audit trail mean something: if an admin could set (or read) a
    nurse's password, "this nurse acknowledged the alert" would not be
    provable. The caller sends the invitation (see ``StaffListView.post``).

    ``locations`` (an iterable of ``Location`` rows, already validated by the
    caller to belong to this organization and to be ones the requester may
    assign into) becomes the new user's ``User.locations`` assignment.

    ``phone`` is optional contact information, not a credential — sign-in is
    by email only. It is stored so a hospital can reach a clinician about an
    alert away from the portal.
    """
    role = Role.objects.get(code=role_code)
    user = User.objects.create_user(
        email=email,
        password=None,
        first_name=first_name,
        last_name=last_name,
        # NULL rather than "" when no number was given: User.phone is unique,
        # so a second staff member onboarded without one would collide with
        # the first on an empty string. Any number of NULLs coexist.
        phone=phone or None,
        role=role,
        requires_password_reset=True,
    )
    user.organization = organization
    user.save(update_fields=["organization", "updated_at"])

    if locations:
        user.locations.set(locations)

    return Staff.objects.create(user=user, employee_id=_next_employee_id(organization))


def can_manage_staff(requester, staff) -> bool:
    """hospital_admin manages every staff member at their hospital; a
    location manager manages only staff assigned to (at least one of) the
    location(s) they themselves manage — mirrors the same Admin-or-own-
    manager split already used for onboarding and for Locations."""
    if requester.role_code == settings.ROLE_HOSPITAL_ADMIN:
        return True
    return staff.user.locations.filter(location_manager=requester).exists()


def deactivate_staff(staff, *, by=None, reason: str = "") -> Staff:
    """Deactivate a staff member — never delete. Blocked while patients are
    still assigned to them (as lead clinician or active care-team member);
    reassign those patients first."""
    count = staff.current_patient_count
    if count:
        msg = (
            f"This staff member can't be deactivated because {count} patient(s) are "
            "currently assigned to them. Reassign those patients before deactivating."
        )
        raise StaffError(msg)
    staff.deactivate(by=by, reason=reason)
    return staff


def reactivate_staff(staff) -> Staff:
    """Restore a previously deactivated staff member."""
    staff.reactivate()
    return staff


def delete_staff(staff) -> None:
    """Hard-delete — mirrors Neuro_RPM's own guard: only a staff member
    already deactivated may be permanently removed, and deleting deletes the
    underlying ``User`` account outright (cascading the ``Staff`` row with
    it), not just the employment record. Anything with a protected reference
    to that user (clinical notes authored, pregnancies led, alerts they
    handled — all deliberately kept forever) blocks this at the database
    level; that refusal is caught and turned into a clean, user-safe error
    instead of a raw 500.
    """
    if staff.is_active:
        raise StaffError("This staff member must be deactivated before they can be deleted.")
    try:
        staff.user.delete()
    except ProtectedError as exc:
        msg = (
            "This staff member cannot be deleted because of protected clinical history — "
            "hard delete only works for a staff member with no such history."
        )
        raise StaffError(msg) from exc
