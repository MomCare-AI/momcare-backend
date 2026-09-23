"""A hospital's own request to close its account — the in-app alternative
to contacting support, existing alongside it rather than replacing it.

One URL, two verbs: POST to ask, GET to check where that ask currently
stands. Acting on the request (approve/dismiss) never happens here — that's
the platform_admin app's job.
"""

import json

import pytest
from django.conf import settings

from momcare_platform.core.organization.models import OrganizationDeactivationRequest

pytestmark = pytest.mark.django_db

URL = "/api/organization/me/deactivation-request/"


def post(client, headers, **fields):
    return client.post(URL, data=json.dumps(fields), content_type="application/json", **headers)


def test_a_hospital_admin_can_request_deactivation(client, make_hospital, auth):
    hospital = make_hospital("Closing Hospital")

    response = post(client, auth(hospital.admin.email), reason="We're merging with another facility.")

    assert response.status_code == 201, response.content
    body = response.json()
    assert body["status"] == OrganizationDeactivationRequest.STATUS_PENDING
    assert body["reason"] == "We're merging with another facility."
    assert body["requested_by_name"] == hospital.admin.get_full_name()
    assert OrganizationDeactivationRequest.objects.filter(organization=hospital.org).exists()


def test_a_reason_is_optional(client, make_hospital, auth):
    hospital = make_hospital("Terse Hospital")

    response = post(client, auth(hospital.admin.email))

    assert response.status_code == 201, response.content
    assert response.json()["reason"] == ""


def test_a_non_admin_cannot_request_deactivation(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Locked Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@locked.test")

    response = post(client, auth(nurse.email), reason="Trying anyway.")

    assert response.status_code == 403
    assert not OrganizationDeactivationRequest.objects.exists()


def test_a_second_request_is_refused_while_one_is_pending(client, make_hospital, auth):
    hospital = make_hospital("Double Ask Hospital")
    post(client, auth(hospital.admin.email), reason="First ask.")

    response = post(client, auth(hospital.admin.email), reason="Second ask.")

    assert response.status_code == 400
    assert OrganizationDeactivationRequest.objects.filter(organization=hospital.org).count() == 1


def test_get_returns_the_most_recent_request(client, make_hospital, auth):
    hospital = make_hospital("Checking Hospital")
    post(client, auth(hospital.admin.email), reason="Please close us.")

    response = client.get(URL, **auth(hospital.admin.email))

    assert response.status_code == 200
    assert response.json()["status"] == OrganizationDeactivationRequest.STATUS_PENDING
    assert response.json()["reason"] == "Please close us."


def test_get_404s_when_no_request_has_ever_been_made(client, make_hospital, auth):
    hospital = make_hospital("Untouched Hospital")

    response = client.get(URL, **auth(hospital.admin.email))

    assert response.status_code == 404


def test_get_is_also_admin_only(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Nurse Checking Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@checking.test")

    response = client.get(URL, **auth(nurse.email))

    assert response.status_code == 403


def test_another_hospitals_request_is_never_visible(client, make_hospital, auth):
    """Tenant isolation for a brand-new table — proven the same way every
    other cross-tenant test in this suite is: two real hospitals, one
    request, checked from the other's own session."""
    ours = make_hospital("Ours Hospital")
    theirs = make_hospital("Theirs Hospital")
    post(client, auth(theirs.admin.email), reason="Theirs, not ours.")

    response = client.get(URL, **auth(ours.admin.email))

    assert response.status_code == 404


def test_an_anonymous_caller_is_refused(client):
    assert client.get(URL).status_code in (401, 403)
    assert post(client, {}, reason="x").status_code in (401, 403)
