"""``/api/organization/me/notifications/`` -- the bell icon's feed, and
marking one read.
"""

import json

import pytest
from django.conf import settings

from momcare_platform.core.organization.models import Notification

pytestmark = pytest.mark.django_db

LIST_URL = "/api/organization/me/notifications/"


def mark_read_url(notification_id):
    return f"/api/organization/me/notifications/{notification_id}/mark-read/"


def make_notification(org, *, message="Test notification.", is_read=False):
    n = Notification.objects.create(
        organization=org,
        notification_type=Notification.TYPE_PATIENT_JOIN_REQUEST,
        message=message,
    )
    if is_read:
        n.mark_read()
    return n


def test_hospital_admin_sees_its_own_notifications(client, make_hospital, auth):
    hospital = make_hospital("Notification List Hospital")
    make_notification(hospital.org, message="First.")
    make_notification(hospital.org, message="Second.")

    response = client.get(LIST_URL, **auth(hospital.admin.email))

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["unread_count"] == 2


def test_a_nurse_can_also_see_the_feed(client, make_hospital, make_staff, auth):
    """Same visibility as the join-request queue itself -- any hospital-side
    role, not admin-only."""
    hospital = make_hospital("Nurse Notification Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@notif.test")
    make_notification(hospital.org)

    response = client.get(LIST_URL, **auth(nurse.email))

    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_notifications_never_cross_hospitals(client, make_hospital, auth):
    alpha = make_hospital("Alpha Notification Hospital")
    beta = make_hospital("Beta Notification Hospital")
    make_notification(beta.org, message="Beta's own business.")

    response = client.get(LIST_URL, **auth(alpha.admin.email))

    body = response.json()
    assert body["count"] == 0
    assert body["unread_count"] == 0


def test_is_read_filter_narrows_the_list(client, make_hospital, auth):
    hospital = make_hospital("Filter Notification Hospital")
    make_notification(hospital.org, message="Unread one.", is_read=False)
    make_notification(hospital.org, message="Already seen.", is_read=True)

    response = client.get(f"{LIST_URL}?is_read=false", **auth(hospital.admin.email))

    body = response.json()
    assert body["count"] == 1
    assert body["results"][0]["message"] == "Unread one."


def test_unread_count_ignores_the_is_read_filter(client, make_hospital, auth):
    """The badge count is always the true unread total, even while viewing
    a filtered (e.g. read-only) slice of the list."""
    hospital = make_hospital("Badge Count Hospital")
    make_notification(hospital.org, is_read=False)
    make_notification(hospital.org, is_read=True)

    response = client.get(f"{LIST_URL}?is_read=true", **auth(hospital.admin.email))

    body = response.json()
    assert body["count"] == 1
    assert body["unread_count"] == 1


def test_marking_read_clears_it_from_the_unread_count(client, make_hospital, auth):
    hospital = make_hospital("Mark Read API Hospital")
    notification = make_notification(hospital.org)

    response = client.post(mark_read_url(notification.id), **auth(hospital.admin.email))
    assert response.status_code == 200
    assert response.json()["is_read"] is True

    follow_up = client.get(LIST_URL, **auth(hospital.admin.email))
    assert follow_up.json()["unread_count"] == 0


def test_cannot_mark_read_another_hospitals_notification(client, make_hospital, auth):
    alpha = make_hospital("Alpha Mark Read Hospital")
    beta = make_hospital("Beta Mark Read Hospital")
    beta_notification = make_notification(beta.org)

    response = client.post(mark_read_url(beta_notification.id), **auth(alpha.admin.email))

    assert response.status_code == 404
    beta_notification.refresh_from_db()
    assert beta_notification.is_read is False


def test_a_patient_cannot_read_the_notification_feed(client, make_hospital, auth):
    from momcare_platform.core.users.models import Role, User  # noqa: PLC0415

    hospital = make_hospital("Patient Block Notification Hospital")
    mother = User.objects.create_user(
        email="mother@notifblock.test",
        password="MotherPass!2026",
        first_name="Mother",
        last_name="Block",
        role=Role.objects.get(code=settings.ROLE_PATIENT),
    )
    mother.organization = hospital.org
    mother.is_email_verified = True
    mother.save(update_fields=["organization", "is_email_verified"])

    response = client.get(LIST_URL, **auth(mother.email, "MotherPass!2026"))

    assert response.status_code == 403


# ── Real end-to-end: sending a join request actually produces a notification ─


def test_sending_a_real_join_request_shows_up_in_the_feed(client, make_hospital, auth):
    from momcare_platform.core.users.models import Role, User  # noqa: PLC0415

    hospital = make_hospital("End To End Notification Hospital")
    applicant = User.objects.create_user(
        email="applicant@notife2e.test",
        password="ApplicantPass!2026",
        first_name="Real",
        last_name="Applicant",
        role=Role.objects.get(code="patient"),
        is_email_verified=True,
    )

    response = client.post(
        "/api/my-requests/",
        data=json.dumps({"organization": str(hospital.org.id), "draft": {"first_name": "Real"}}),
        content_type="application/json",
        **auth(applicant.email, "ApplicantPass!2026"),
    )
    assert response.status_code == 201

    feed = client.get(LIST_URL, **auth(hospital.admin.email))
    body = feed.json()
    assert body["count"] == 1
    assert body["results"][0]["related_object_id"] == response.json()["id"]
