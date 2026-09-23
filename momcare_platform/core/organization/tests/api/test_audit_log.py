"""A hospital's own PHI-access trail.

``AuditLogMiddleware`` writes one row per PHI-touching request automatically
(see ``core/common/middleware.py``) — this endpoint is the first place any
of those rows are ever read back. Isolation matters most here: an audit
trail that leaked across hospitals would itself be a PHI leak.
"""

import json
from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from momcare_platform.core.organization.models import AuditLog

pytestmark = pytest.mark.django_db

URL = "/api/organization/me/audit-log/"


def get(client, headers, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(f"{URL}?{query}" if query else URL, **headers)


def make_entry(user, *, action=AuditLog.ACTION_READ, resource="patients", resource_id="abc"):
    return AuditLog.objects.create(
        user=user,
        action=action,
        resource=resource,
        resource_id=resource_id,
        ip_address="127.0.0.1",
        endpoint=f"/api/{resource}/{resource_id}/",
    )


# ── Real end-to-end: the middleware actually writes what this endpoint reads ─


def test_a_real_request_to_a_phi_endpoint_shows_up_here(client, make_hospital, auth):
    """No manually-created fixture — a genuine GET /api/patients/ has to be
    the thing that produces the row this endpoint returns."""
    hospital = make_hospital("Real Trail Hospital")
    headers = auth(hospital.admin.email)

    client.get("/api/patients/", **headers)

    response = get(client, headers)

    assert response.status_code == 200
    assert response.json()["count"] >= 1
    row = response.json()["results"][0]
    assert row["resource"] == "patients"
    assert row["action"] == "READ"
    assert row["user_email"] == hospital.admin.email


# ── Permissions ──────────────────────────────────────────────────────────────


def test_a_hospital_admin_can_read_it(client, make_hospital, auth):
    hospital = make_hospital("Admin Read Hospital")
    make_entry(hospital.admin)

    response = get(client, auth(hospital.admin.email))

    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_a_nurse_cannot_read_it(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Nurse Blocked Hospital")
    make_staff(hospital.org, settings.ROLE_NURSE, "nurse@nurseblocked.test")

    response = get(client, auth("nurse@nurseblocked.test"))

    assert response.status_code == 403


def test_a_provider_cannot_read_it(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Provider Blocked Hospital")
    make_staff(hospital.org, settings.ROLE_PROVIDER, "doc@providerblocked.test")

    response = get(client, auth("doc@providerblocked.test"))

    assert response.status_code == 403


# ── Isolation — the part that matters most ──────────────────────────────────


def test_a_hospital_never_sees_another_hospitals_entries(client, make_hospital, auth):
    alpha = make_hospital("Alpha Trail Hospital")
    beta = make_hospital("Beta Trail Hospital")
    make_entry(alpha.admin, resource="patients", resource_id="alpha-1")
    make_entry(beta.admin, resource="patients", resource_id="beta-1")

    response = get(client, auth(alpha.admin.email))

    assert response.status_code == 200
    ids = [r["resource_id"] for r in response.json()["results"]]
    assert "alpha-1" in ids
    assert "beta-1" not in ids


def test_an_entry_with_no_user_is_invisible_to_every_hospital(client, make_hospital, auth):
    """user=None (account since deleted) cannot be attributed to any
    hospital — it must not surface under anyone's scope."""
    hospital = make_hospital("Orphan Entry Hospital")
    make_entry(None, resource="patients", resource_id="orphaned")
    make_entry(hospital.admin, resource="patients", resource_id="attributable")

    response = get(client, auth(hospital.admin.email))

    ids = [r["resource_id"] for r in response.json()["results"]]
    assert "orphaned" not in ids
    assert "attributable" in ids


# ── Filtering ────────────────────────────────────────────────────────────────


def test_filtering_by_action(client, make_hospital, auth):
    hospital = make_hospital("Filter Action Hospital")
    make_entry(hospital.admin, action=AuditLog.ACTION_READ, resource_id="r1")
    make_entry(hospital.admin, action=AuditLog.ACTION_DELETE, resource_id="d1")

    response = get(client, auth(hospital.admin.email), action="DELETE")

    results = response.json()["results"]
    assert len(results) == 1
    assert results[0]["resource_id"] == "d1"


def test_filtering_by_resource(client, make_hospital, auth):
    hospital = make_hospital("Filter Resource Hospital")
    make_entry(hospital.admin, resource="patients", resource_id="p1")
    make_entry(hospital.admin, resource="alerts", resource_id="a1")

    response = get(client, auth(hospital.admin.email), resource="alerts")

    results = response.json()["results"]
    assert len(results) == 1
    assert results[0]["resource_id"] == "a1"


# ── Ordering ─────────────────────────────────────────────────────────────────


def test_newest_entries_come_first(client, make_hospital, auth):
    hospital = make_hospital("Ordering Hospital")
    older = make_entry(hospital.admin, resource_id="older")
    older.timestamp = timezone.now() - timedelta(hours=1)
    older.save(update_fields=["timestamp"])
    make_entry(hospital.admin, resource_id="newer")

    response = get(client, auth(hospital.admin.email))

    ids = [r["resource_id"] for r in response.json()["results"]]
    assert ids[0] == "newer"
    assert ids[-1] == "older"


# ── Not a write surface ──────────────────────────────────────────────────────


def test_there_is_no_way_to_write_an_entry_through_the_api(client, make_hospital, auth):
    hospital = make_hospital("No Write Hospital")

    response = client.post(
        URL,
        data=json.dumps({"action": "DELETE", "resource": "patients", "resource_id": "x"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 405
