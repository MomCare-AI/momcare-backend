"""Deactivating a hospital — a platform-admin-only, permanent-leaning decision,
distinct from ``set_review_status(SUSPENDED)``'s reversible, review-driven
block. No API surface exists for the deactivation itself; it's exercised the
same way ``set_review_status`` already is elsewhere in this test suite — by
calling the service function directly, the same thing the admin actions call.

The request/approve/dismiss cycle below is different: ``request_deactivation``
*is* reachable from the API (see ``tests/api/test_deactivation_request.py``
for that side) — these tests cover the service layer directly, the same way
the plain deactivate/reactivate tests above do.
"""

import pytest

from momcare_platform.core.organization.models import Organization, OrganizationDeactivationRequest
from momcare_platform.core.organization.services import (
    DeactivationRequestError,
    approve_deactivation_request,
    deactivate_organization,
    dismiss_deactivation_request,
    reactivate_organization,
    request_deactivation,
)

pytestmark = pytest.mark.django_db


def test_deactivating_a_hospital_records_who_and_why(make_hospital):
    hospital = make_hospital("Closing Hospital")

    deactivate_organization(hospital.org, by=hospital.admin, reason="Hospital has closed.")

    hospital.org.refresh_from_db()
    assert hospital.org.is_active is False
    assert hospital.org.deactivated_at is not None
    assert hospital.org.deactivated_by_id == hospital.admin.id
    assert hospital.org.deactivation_reason == "Hospital has closed."


def test_a_deactivated_hospital_cannot_authenticate(client, make_hospital):
    hospital = make_hospital("Deactivated Hospital")
    deactivate_organization(hospital.org)

    response = client.post(
        "/api/auth/login/",
        data={"email": hospital.admin.email, "password": hospital.password},
        content_type="application/json",
    )

    assert response.status_code == 403


def test_reactivating_clears_the_deactivation_fields(make_hospital):
    hospital = make_hospital("Reopened Hospital")
    deactivate_organization(hospital.org, reason="Temporary closure.")

    reactivate_organization(hospital.org)

    hospital.org.refresh_from_db()
    assert hospital.org.is_active is True
    assert hospital.org.deactivated_at is None
    assert hospital.org.deactivation_reason == ""


def test_reactivating_restores_authentication(client, make_hospital):
    hospital = make_hospital("Restored Hospital")
    deactivate_organization(hospital.org)
    assert (
        client.post(
            "/api/auth/login/",
            data={"email": hospital.admin.email, "password": hospital.password},
            content_type="application/json",
        ).status_code
        == 403
    )

    reactivate_organization(hospital.org)

    response = client.post(
        "/api/auth/login/",
        data={"email": hospital.admin.email, "password": hospital.password},
        content_type="application/json",
    )
    assert response.status_code == 200


def test_deactivation_is_independent_of_suspension(make_hospital):
    """The whole point of keeping the two axes separate: a suspended hospital
    is still, distinctly, active; deactivating never touches ``status``."""
    hospital = make_hospital("Suspended Not Closed Hospital")
    hospital.org.set_review_status(Organization.STATUS_SUSPENDED)

    deactivate_organization(hospital.org, reason="Also closed, separately.")

    hospital.org.refresh_from_db()
    assert hospital.org.status == Organization.STATUS_SUSPENDED
    assert hospital.org.is_active is False


# -- Requesting deactivation ---------------------------------------------------


def test_requesting_deactivation_creates_a_pending_row(make_hospital):
    hospital = make_hospital("Asking Hospital")

    deactivation_request = request_deactivation(hospital.org, by=hospital.admin, reason="Closing down.")

    assert deactivation_request.status == OrganizationDeactivationRequest.STATUS_PENDING
    assert deactivation_request.requested_by_id == hospital.admin.id
    assert deactivation_request.reason == "Closing down."
    # Requesting never deactivates by itself — only approval does.
    hospital.org.refresh_from_db()
    assert hospital.org.is_active is True


def test_requesting_twice_while_pending_is_refused(make_hospital):
    hospital = make_hospital("Repeat Asker Hospital")
    request_deactivation(hospital.org, by=hospital.admin)

    with pytest.raises(DeactivationRequestError):
        request_deactivation(hospital.org, by=hospital.admin)

    assert OrganizationDeactivationRequest.objects.filter(organization=hospital.org).count() == 1


def test_requesting_again_after_a_dismissal_is_allowed(make_hospital):
    """A dismissed request doesn't block a genuine future ask — only a
    *pending* one does (see the model's own conditional constraint)."""
    hospital = make_hospital("Second Chance Hospital")
    first = request_deactivation(hospital.org, by=hospital.admin, reason="First try.")
    dismiss_deactivation_request(first, by=hospital.admin)

    second = request_deactivation(hospital.org, by=hospital.admin, reason="Second try.")

    assert second.pk != first.pk
    assert OrganizationDeactivationRequest.objects.filter(organization=hospital.org).count() == 2


# -- Approving / dismissing -----------------------------------------------------


def test_approving_a_request_deactivates_the_hospital(make_hospital):
    hospital = make_hospital("Approved Closure Hospital")
    deactivation_request = request_deactivation(hospital.org, by=hospital.admin, reason="Shutting down.")

    approve_deactivation_request(deactivation_request, by=hospital.admin, note="Confirmed by phone.")

    deactivation_request.refresh_from_db()
    hospital.org.refresh_from_db()
    assert deactivation_request.status == OrganizationDeactivationRequest.STATUS_APPROVED
    assert deactivation_request.review_note == "Confirmed by phone."
    assert deactivation_request.reviewed_at is not None
    assert hospital.org.is_active is False
    assert hospital.org.deactivation_reason == "Shutting down."


def test_dismissing_a_request_leaves_the_hospital_untouched(make_hospital):
    hospital = make_hospital("Declined Closure Hospital")
    deactivation_request = request_deactivation(hospital.org, by=hospital.admin, reason="Having second thoughts.")

    dismiss_deactivation_request(deactivation_request, by=hospital.admin, note="Talked them out of it.")

    deactivation_request.refresh_from_db()
    hospital.org.refresh_from_db()
    assert deactivation_request.status == OrganizationDeactivationRequest.STATUS_DISMISSED
    assert hospital.org.is_active is True
