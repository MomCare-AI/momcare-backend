"""``Notification`` model and its producer signal
(``organization.signals.notify_hospital_of_join_request``).
"""

import pytest

from momcare_platform.core.organization.models import Notification
from momcare_platform.core.patients.models import PatientJoinRequest
from momcare_platform.core.users.models import Role, User

pytestmark = pytest.mark.django_db


def make_applicant(email="applicant@e2etest.test"):
    return User.objects.create_user(
        email=email,
        password="TestPass!2026",
        first_name="Applicant",
        last_name="Woman",
        role=Role.objects.get(code="patient"),
        is_email_verified=True,
    )


def test_creating_a_join_request_creates_a_notification(make_hospital):
    hospital = make_hospital("Notify Signal Hospital")
    applicant = make_applicant()

    join_request = PatientJoinRequest.objects.create(
        user=applicant,
        organization=hospital.org,
        draft={"first_name": "Applicant"},
    )

    notification = Notification.objects.get(organization=hospital.org)
    assert notification.notification_type == Notification.TYPE_PATIENT_JOIN_REQUEST
    assert notification.related_object_id == join_request.id
    assert notification.is_read is False
    assert "Applicant Woman" in notification.message


def test_deciding_a_join_request_does_not_create_a_second_notification(make_hospital):
    """Only creation fires the signal -- a status change (approve/reject) is
    not itself a new "you have a request" event."""
    hospital = make_hospital("No Duplicate Signal Hospital")
    applicant = make_applicant()
    join_request = PatientJoinRequest.objects.create(
        user=applicant,
        organization=hospital.org,
        draft={"first_name": "Applicant"},
    )
    assert Notification.objects.filter(organization=hospital.org).count() == 1

    join_request.status = PatientJoinRequest.STATUS_REJECTED
    join_request.save()

    assert Notification.objects.filter(organization=hospital.org).count() == 1


def test_mark_read_sets_timestamp_and_is_idempotent(make_hospital):
    hospital = make_hospital("Mark Read Hospital")
    notification = Notification.objects.create(
        organization=hospital.org,
        notification_type=Notification.TYPE_PATIENT_JOIN_REQUEST,
        message="Test notification.",
    )
    assert notification.is_read is False
    assert notification.read_at is None

    notification.mark_read()
    notification.refresh_from_db()
    assert notification.is_read is True
    first_read_at = notification.read_at
    assert first_read_at is not None

    notification.mark_read()  # calling again must not move the timestamp
    notification.refresh_from_db()
    assert notification.read_at == first_read_at
