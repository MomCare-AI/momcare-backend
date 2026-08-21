"""Transactional service functions for staff onboarding."""

from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from momcare_platform.core.staff.models import Staff, StaffInvite
from momcare_platform.core.users.models import User

# Roles a hospital admin may hand out. Deliberately excludes platform_admin
# (Momcare's own staff, created by createsuperuser) and patient (enrolled by
# clinical staff, never invited into the hospital's own team).
INVITABLE_ROLE_CODES = frozenset(
    {
        settings.ROLE_HOSPITAL_ADMIN,
        settings.ROLE_PROVIDER,
        settings.ROLE_NURSE,
        settings.ROLE_CARE_MANAGER,
    },
)


class InviteError(Exception):
    """Raised when an invite cannot be accepted; message is safe to show a user."""


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
def accept_invite(*, token: str, password: str, first_name: str = "", last_name: str = "") -> User:
    """Turn a valid invite into a real hospital user + employment record.

    The organization and role come from the invite row, never from the request,
    so accepting cannot be used to join a different hospital or claim a
    different role. Locks the invite row for the duration so two concurrent
    submissions of the same link cannot both create a user.
    """
    try:
        invite = StaffInvite.objects.select_for_update().select_related("organization", "role").get(token=token)
    except StaffInvite.DoesNotExist as exc:
        raise InviteError("This invitation link is not valid.") from exc

    if invite.accepted_at is not None:
        raise InviteError("This invitation has already been used.")
    if invite.revoked_at is not None:
        raise InviteError("This invitation has been revoked.")
    if invite.is_expired:
        raise InviteError("This invitation has expired. Ask your hospital admin to send a new one.")
    if not invite.organization.can_authenticate:
        raise InviteError("This hospital is not currently active. Contact your hospital admin.")
    if User.objects.filter(email__iexact=invite.email).exists():
        raise InviteError("An account already exists for this email. Try signing in instead.")

    user = User.objects.create_user(
        email=invite.email,
        password=password,
        first_name=first_name or invite.first_name,
        last_name=last_name or invite.last_name,
        role=invite.role,
    )
    user.organization = invite.organization
    user.save(update_fields=["organization", "updated_at"])

    Staff.objects.create(user=user, employee_id=_next_employee_id(invite.organization))

    invite.accepted_at = timezone.now()
    invite.accepted_user = user
    invite.save(update_fields=["accepted_at", "accepted_user", "updated_at"])

    return user
