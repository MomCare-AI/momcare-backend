"""Organization service functions.

Actually deactivating/reactivating an organization is always a
platform-admin-only act — see ``deactivate_organization``/
``reactivate_organization`` below. A hospital can only ever *ask*, via
``request_deactivation``; approving that ask still routes through the same
``deactivate_organization`` a platform admin's own manual action calls, so
there is one behaviour to reason about regardless of which path reached it.

Unlike ``Location``/``Staff`` deactivation elsewhere in this codebase, there
is no "zero active dependents" guard on the deactivation itself — those
guards exist because a Location or a Staff member is a *middle* node in the
tenancy hierarchy, with somewhere else within the same hospital for their
dependents to move to. Organization is the top of that hierarchy — there is
no "move this hospital's patients to another organization" operation, and
closing a hospital's platform access was never meant to require emptying it
out first.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from momcare_platform.core.organization.models import OrganizationDeactivationRequest


class DeactivationRequestError(Exception):
    """Raised when a deactivation request cannot proceed; message is user-safe."""


def deactivate_organization(organization, *, by=None, reason: str = ""):
    """Retire a hospital's platform access — a distinct, permanent-leaning
    decision from ``set_review_status(SUSPENDED)``, which is a reversible,
    review-driven block. Named as a function (rather than calling
    ``organization.deactivate()`` directly from the admin) so there is one
    place to add a rule later, if one ever turns out to be needed, without
    touching the admin again.
    """
    organization.deactivate(by=by, reason=reason)
    return organization


def reactivate_organization(organization):
    """Restore a previously deactivated hospital's platform access."""
    organization.reactivate()
    return organization


def request_deactivation(organization, *, by, reason: str = "") -> OrganizationDeactivationRequest:
    """A hospital_admin's own ask to close their account.

    Only ever creates the request — it never deactivates anything by itself.
    Blocked while one is already pending, so asking twice cannot pile up two
    rows waiting on the same decision (the model's own constraint guards
    this too; this check turns the race into a clean, readable error rather
    than a raw IntegrityError).
    """
    if organization.deactivation_requests.filter(status=OrganizationDeactivationRequest.STATUS_PENDING).exists():
        raise DeactivationRequestError("A deactivation request for this hospital is already pending review.")

    return OrganizationDeactivationRequest.objects.create(
        organization=organization,
        requested_by=by,
        reason=reason,
    )


@transaction.atomic
def approve_deactivation_request(deactivation_request, *, by, note: str = "") -> OrganizationDeactivationRequest:
    """A platform admin agrees — closes the request and actually deactivates
    the hospital, in one transaction, so there is no window where the
    request reads "approved" but the hospital can still sign in."""
    deactivation_request.status = OrganizationDeactivationRequest.STATUS_APPROVED
    deactivation_request.reviewed_by = by
    deactivation_request.reviewed_at = timezone.now()
    deactivation_request.review_note = note
    deactivation_request.save(
        update_fields=["status", "reviewed_by", "reviewed_at", "review_note", "updated_at"],
    )
    deactivate_organization(
        deactivation_request.organization,
        by=by,
        reason=deactivation_request.reason or "Deactivation requested by the hospital.",
    )
    return deactivation_request


def dismiss_deactivation_request(deactivation_request, *, by, note: str = "") -> OrganizationDeactivationRequest:
    """A platform admin declines — the hospital stays exactly as it was."""
    deactivation_request.status = OrganizationDeactivationRequest.STATUS_DISMISSED
    deactivation_request.reviewed_by = by
    deactivation_request.reviewed_at = timezone.now()
    deactivation_request.review_note = note
    deactivation_request.save(
        update_fields=["status", "reviewed_by", "reviewed_at", "review_note", "updated_at"],
    )
    return deactivation_request
