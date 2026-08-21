"""The tenant review gate.

Self-registration creates a PENDING hospital. Until a platform admin approves
it, nobody inside that hospital may authenticate — registration is an
application, not a sign-up.
"""

import pytest

from momcare_platform.core.organization.models import Organization

pytestmark = pytest.mark.django_db

LOGIN = "/api/auth/login/"


def _login(client, email, password="TestPass!2026"):
    return client.post(
        LOGIN,
        data={"email": email, "password": password},
        content_type="application/json",
    )


def test_pending_hospital_cannot_log_in(client, make_hospital):
    """A hospital awaiting review must not get a token, even with correct credentials."""
    hospital = make_hospital("Pending Clinic", status=Organization.STATUS_PENDING)

    response = _login(client, hospital.admin.email)

    assert response.status_code == 403
    body = response.json()
    assert body["org_status"] == Organization.STATUS_PENDING
    assert "access" not in body, "a pending hospital must never receive a token"
    assert "under review" in body["detail"].lower()


def test_approved_hospital_can_log_in(client, make_hospital):
    hospital = make_hospital("Approved Clinic", status=Organization.STATUS_APPROVED)

    response = _login(client, hospital.admin.email)

    assert response.status_code == 200
    body = response.json()
    assert body["access"], "an approved hospital must receive an access token"
    assert body["user"]["organization_name"] == "Approved Clinic"


@pytest.mark.parametrize(
    "status",
    [Organization.STATUS_REJECTED, Organization.STATUS_SUSPENDED],
)
def test_blocked_statuses_cannot_log_in(client, make_hospital, status):
    """Rejected and suspended are distinct from pending, and both deny access."""
    hospital = make_hospital(f"Blocked {status}", status=status)

    response = _login(client, hospital.admin.email)

    assert response.status_code == 403
    assert response.json()["org_status"] == status


def test_approval_unlocks_a_previously_pending_hospital(client, make_hospital):
    """The gate is a state check, not a one-off decision at registration."""
    hospital = make_hospital("Later Approved", status=Organization.STATUS_PENDING)
    assert _login(client, hospital.admin.email).status_code == 403

    hospital.org.set_review_status(Organization.STATUS_APPROVED)

    assert _login(client, hospital.admin.email).status_code == 200


def test_registration_issues_no_token(client):
    """Registration returns an application receipt, never credentials."""
    response = client.post(
        "/api/auth/register/",
        data={
            "first_name": "Bilal",
            "last_name": "Ahmed",
            "email": "owner@newhospital.test",
            "password": "BrandNewPass!2026",
            "org_name": "New Hospital",
            "org_email": "info@newhospital.test",
            "org_phone": "0511111111",
            "address_line1": "1 Test Road",
            "city": "Islamabad",
            "state": "ICT",
            "postal_code": "44000",
            "country": "Pakistan",
        },
        content_type="application/json",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == Organization.STATUS_PENDING
    assert "access" not in body
    assert "refresh_token" not in response.cookies
