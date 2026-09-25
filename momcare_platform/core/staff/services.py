"""Staff onboarding & lifecycle services."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.db.models.deletion import ProtectedError
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework import serializers

from momcare_platform.core.monitoring.models import MonitoringNote, MonitoringSession
from momcare_platform.core.monitoring.services import monitoring_period_totals
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


# Preset rolling-window codes for the staff audit report -- "how far back
# from right now," not a specific calendar month to browse (see
# docs/design/2026-09-25-staff-audit-report-design.md).
AUDIT_PERIODS = {
    "2d": 2,
    "week": 7,
    "month": 30,
    "3month": 90,
    "6month": 180,
    "year": 365,
    "2year": 730,
}


def resolve_audit_period(code: str) -> tuple:
    """``(start, end)`` for a preset audit-report window, ending now.

    UTC, not a per-location timezone -- unlike Patient, a Staff row has no
    single canonical location (locations live on User, many-to-many), and a
    rolling N-day window has no calendar-day boundary to get right the way
    month-browsing does.
    """
    if code not in AUDIT_PERIODS:
        raise serializers.ValidationError(
            {"period": [f"Must be one of: {', '.join(AUDIT_PERIODS)}."]},
        )
    end = timezone.now()
    start = end - timedelta(days=AUDIT_PERIODS[code])
    return start, end


def compute_audit_report(staff: Staff, *, start, end) -> dict:
    """Assemble the staff audit report for ``staff`` within [``start``,
    ``end``] -- caseload, monitoring time (with a day-by-day distribution),
    call outcomes, and alerts handled. No RPM/CCM split and no "compliance %"
    -- neither concept has a MomCare equivalent (see the design doc).

    Alert is resolved via the app registry, not a static import: it lives in
    ``modules.pregnancy.alerts``, a module `core` must never import (the
    `core must not import modules` import-linter contract) -- same pattern
    used elsewhere in `core.patients` for `RiskAssessment`.
    """
    from django.apps import apps as django_apps  # noqa: PLC0415

    Alert = django_apps.get_model("alerts", "Alert")

    user = staff.user

    monitoring_time = monitoring_period_totals(added_by=user, start=start, end=end)
    distribution = list(
        MonitoringSession.objects.filter(added_by=user, recorded_at__gte=start, recorded_at__lte=end)
        .annotate(day=TruncDate("recorded_at"))
        .values("day")
        .annotate(seconds=Sum("duration_seconds"))
        .order_by("day"),
    )
    monitoring_time["distribution"] = [
        {"date": row["day"].isoformat(), "seconds": row["seconds"]} for row in distribution
    ]

    notes = MonitoringNote.objects.filter(added_by=user, recorded_at__gte=start, recorded_at__lte=end)
    two_way_count = notes.filter(two_way_communication=True).count()
    voicemail_count = notes.filter(left_voicemail=True).count()
    total_calls = two_way_count + voicemail_count
    call_success_rate = round(two_way_count / total_calls, 2) if total_calls else 0

    acknowledged_count = Alert.objects.filter(
        acknowledged_by=user,
        acknowledged_at__gte=start,
        acknowledged_at__lte=end,
    ).count()
    resolved_count = Alert.objects.filter(
        resolved_by=user,
        resolved_at__gte=start,
        resolved_at__lte=end,
    ).count()

    return {
        "staff": {
            "id": str(staff.id),
            "name": user.get_full_name(),
            "role": user.role_code,
            "employee_id": staff.employee_id,
        },
        "total_patients": staff.current_patient_count,
        "monitoring_time": monitoring_time,
        "call_outcomes": {
            "two_way_count": two_way_count,
            "voicemail_count": voicemail_count,
            "call_success_rate": call_success_rate,
        },
        "alerts_handled": {
            "acknowledged_count": acknowledged_count,
            "resolved_count": resolved_count,
        },
    }


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
