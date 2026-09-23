"""Transactional email.

Every send here is **best-effort**: a mail server being down must never fail a
registration or roll back an approval. Failures are logged, not raised.

Plain text by design — these are short operational notices, they must survive
any client, and a hospital admin on a slow connection should not be waiting on
an HTML template.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)

# From settings, not hardcoded. It was momcare.pk — a domain this project does
# not own — so every email invited people to write to an address that bounces.
SUPPORT_EMAIL = settings.SUPPORT_EMAIL


def _send(subject: str, body: str, to: str) -> bool:
    """Send one message. Returns whether it went out; never raises."""
    if not to:
        return False
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[to],
            fail_silently=False,
        )
    except Exception:  # noqa: BLE001 - mail must never break the caller
        logger.exception("Failed to send %r to %s", subject, to)
        return False
    return True


def send_application_received(user, organization) -> bool:
    """Confirms the application landed, and sets expectations about the wait.

    Also serves as a reachability check: if this never arrives, the applicant
    knows to correct their address before the approval notice is sent to it.

    Echoes back exactly what was submitted (address, phone, org email) —
    deliberately, so a typo made in the registration form is visible to the
    applicant in writing, not discovered later when the wrong number gets
    called during review. No timeframe is promised for the decision, and no
    tracking id/status-check exists here on purpose: a status check already
    exists for free (trying to sign in returns the current review state),
    and a second, unauthenticated one would be a real, if small,
    information-disclosure surface for no real gain over that.
    """
    address_line = organization.address_line1
    if organization.address_line2:
        address_line += f", {organization.address_line2}"
    address = (
        f"{address_line}\n{organization.city}, {organization.state} {organization.postal_code}\n{organization.country}"
    )

    return _send(
        subject=f"MomCare: we received your application for {organization.name}",
        body=(
            f"Hello {user.first_name or 'there'},\n\n"
            f"Thank you for registering {organization.name} on MomCare.\n\n"
            "What you submitted\n"
            "-------------------\n"
            f"Organization: {organization.name}\n"
            f"Address: {address}\n"
            f"Organization phone: {organization.phone}\n"
            f"Organization email: {organization.email}\n"
            f"Sign-in email: {user.email}\n\n"
            "What happens next\n"
            "-----------------\n"
            "A platform administrator will verify your hospital before granting access.\n\n"
            "You will not be able to sign in until that review is complete. We will "
            "email you as soon as a decision is made.\n\n"
            f"If any of the details above are wrong, or if you did not register this "
            f"hospital, please contact {SUPPORT_EMAIL}.\n\n"
            "— The MomCare team"
        ),
        to=user.email,
    )


def send_application_approved(user, organization) -> bool:
    return _send(
        subject=f"MomCare: {organization.name} has been approved",
        body=(
            f"Hello {user.first_name or 'there'},\n\n"
            f"{organization.name} has been approved. You can now sign in with "
            f"{user.email} and begin onboarding your clinical team.\n\n"
            "Doctors, nurses and care managers do not register themselves — you onboard "
            "them from Doctors & Staff, and each new account's sign-in details are "
            "emailed to them directly.\n\n"
            "— The MomCare team"
        ),
        to=user.email,
    )


def send_application_rejected(user, organization, note: str = "") -> bool:
    reason = f"\nReviewer's note:\n{note}\n" if note else ""
    return _send(
        subject=f"MomCare: application for {organization.name} was not approved",
        body=(
            f"Hello {user.first_name or 'there'},\n\n"
            f"After review, the application for {organization.name} has not been approved, "
            "so sign-in remains unavailable.\n"
            f"{reason}\n"
            f"If you believe this is a mistake, reply to {SUPPORT_EMAIL} with your licence "
            "details and we will look again.\n\n"
            "— The MomCare team"
        ),
        to=user.email,
    )


def send_staff_invitation(user, invite_url: str, organization) -> bool:
    """The invitation that activates a newly-created staff account.

    Carries a one-time LINK, never a password. The account already exists —
    role and locations are assigned — but its password is unusable, so it
    cannot be signed into until the person follows this link and chooses one
    themselves.

    That is the point: nobody, including the hospital admin who created the
    account, ever knows the password. A plain-text password would sit in an
    inbox indefinitely and would mean an admin could sign in as a clinician —
    which would make "this nurse acknowledged the alert" unprovable.

    The link stops working once used: Django's token generator derives the
    token partly from the current password hash, so setting a password
    invalidates it.
    """
    return _send(
        subject=f"Activate your MomCare account for {organization.name}",
        body=(
            f"Hello {user.first_name or 'there'},\n\n"
            f"An account has been created for you on MomCare, for {organization.name}, "
            f"as {user.role.name}.\n\n"
            "To finish setting it up, choose your own password here:\n\n"
            f"{invite_url}\n\n"
            "The link can only be used once, and expires after a few days. If it has "
            'expired by the time you get to it, use "Forgot password" on the sign-in '
            f"screen with this address ({user.email}) and you will be sent a fresh one.\n\n"
            "Nobody at MomCare — including your hospital administrator — can see your "
            "password once you set it.\n\n"
            f"If you were not expecting this account, contact {SUPPORT_EMAIL}.\n\n"
            "— The MomCare team"
        ),
        to=user.email,
    )


def send_password_reset(user, reset_url: str) -> bool:
    """The link that lets somebody back into their own account.

    Sent to an address that may not belong to the person who asked — anyone can
    type an email into a reset form — so it says what to do if the request was
    not theirs, and names no detail about the account beyond the address it
    arrived at.
    """
    return _send(
        subject="Reset your MomCare password",
        body=(
            f"Hello {user.first_name or 'there'},\n\n"
            "Someone asked to reset the password for the MomCare account using "
            "this email address.\n\n"
            "Open the link below to choose a new one:\n\n"
            f"{reset_url}\n\n"
            "The link works once and expires in one hour.\n\n"
            "If this was not you, no action is needed — your password has not "
            "changed and this link can be ignored. If you receive these "
            f"repeatedly, contact {SUPPORT_EMAIL}.\n\n"
            "— The MomCare team"
        ),
        to=user.email,
    )


def send_email_otp(user, code: str) -> bool:
    """The six-digit code that proves a self-registered patient controls the
    address she signed up with. See ``core.users.models.EmailVerificationCode``
    for how the code itself is generated, hashed and expired."""
    return _send(
        subject="Confirm your MomCare account",
        body=(
            f"Hello {user.first_name or 'there'},\n\n"
            "Use this code to confirm your email address and finish creating "
            "your MomCare account:\n\n"
            f"    {code}\n\n"
            "It expires in 15 minutes. If you did not try to create an "
            f"account, no action is needed — contact {SUPPORT_EMAIL} if you "
            "receive these repeatedly.\n\n"
            "— The MomCare team"
        ),
        to=user.email,
    )
