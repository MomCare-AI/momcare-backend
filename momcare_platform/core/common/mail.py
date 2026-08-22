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

SUPPORT_EMAIL = "support@momcare.pk"


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
    """
    return _send(
        subject=f"MomCare: we received your application for {organization.name}",
        body=(
            f"Hello {user.first_name or 'there'},\n\n"
            f"Thank you for registering {organization.name} on MomCare.\n\n"
            "What happens next\n"
            "-----------------\n"
            "A platform administrator will verify your hospital against the register of "
            "the authority that issued your licence, and may telephone you on a number "
            "published by that authority to confirm your role.\n\n"
            "You will not be able to sign in until that review is complete. We will "
            "email you as soon as a decision is made.\n\n"
            f"Licence submitted: {organization.license_no or 'not provided'}\n"
            f"Sign-in email: {user.email}\n\n"
            f"If you did not register this hospital, please contact {SUPPORT_EMAIL}.\n\n"
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
            f"{user.email} and begin inviting your clinical team.\n\n"
            "Doctors, nurses and care managers do not register themselves — you invite "
            "them from Doctors & Staff, and each person sets their own password.\n\n"
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


def send_staff_invitation(invite, accept_url: str) -> bool:
    """Emailing the invite is optional — an admin may equally send the link over
    WhatsApp or hand it over in person, which is often how it works in practice."""
    inviter = invite.invited_by.get_full_name() if invite.invited_by else "A hospital administrator"
    return _send(
        subject=f"You've been invited to join {invite.organization.name} on MomCare",
        body=(
            f"Hello {invite.first_name or 'there'},\n\n"
            f"{inviter} has invited you to join {invite.organization.name} on MomCare "
            f"as {invite.role.name}.\n\n"
            "Open the link below to set your own password and finish joining:\n\n"
            f"{accept_url}\n\n"
            "This link works once and expires on "
            f"{invite.expires_at.strftime('%d %B %Y')}.\n\n"
            f"If you were not expecting this, you can ignore it or contact {SUPPORT_EMAIL}.\n\n"
            "— The MomCare team"
        ),
        to=invite.email,
    )


def send_alert_notification(alert, user, tier: int) -> bool:
    """Tell one person that a patient needs attention.

    Terse on purpose. This lands on a phone, often at night, and the only
    things that decide whether someone gets up are who, how bad, and why. The
    portal holds the detail; this is the interrupt.

    The patient is named because the recipient already has clinical access to
    her record — but nothing beyond her name, gestation and the findings goes
    into an email, since mail leaves systems we control.
    """
    from momcare_platform.core.alerts import escalation  # noqa: PLC0415

    patient = alert.pregnancy.patient
    reasons = "\n".join(f"  - {reason}" for reason in alert.reasons) or "  - Outside clinical range."
    escalated = (
        f"\nThis alert reached you because it was escalated to the "
        f"{escalation.tier_label(tier).lower()}.\n"
        if tier != escalation.TIER_CLINICIAN
        else ""
    )

    return _send(
        subject=f"MomCare {alert.level.upper()}: {patient.full_name}",
        body=(
            f"{patient.full_name} needs review.\n\n"
            f"Level:     {alert.level.upper()}\n"
            f"Gestation: {alert.pregnancy.gestational_age_display or 'unknown'}\n"
            f"MRN:       {patient.mrn or 'not assigned'}\n\n"
            f"Why:\n{reasons}\n"
            f"{escalated}\n"
            "Open MomCare to review the readings and acknowledge this alert.\n\n"
            "This is decision support from monitored vitals, not a diagnosis.\n\n"
            "- MomCare"
        ),
        to=user.email,
    )
