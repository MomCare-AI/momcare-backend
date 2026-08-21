"""Applicants are told what happened to their application.

The registration screen promises a confirmation and a decision. An applicant
who is told to wait has no other way of learning one was made, so these are
part of the product's contract, not a nicety.
"""

import json

import pytest
from django.core import mail

from momcare_platform.core.organization.models import Organization

pytestmark = pytest.mark.django_db


REGISTRATION = {
    "first_name": "Bilal",
    "last_name": "Ahmed",
    "email": "owner@sunrise.test",
    "password": "BrandNewPass!2026",
    "org_name": "Sunrise Maternity",
    "org_email": "info@sunrise.test",
    "org_phone": "0511111111",
    "address_line1": "22 Blue Area",
    "city": "Islamabad",
    "state": "ICT",
    "postal_code": "44000",
    "country": "Pakistan",
    "license_no": "IHRA-2026-115",
    "license_authority": "ihra",
}


def test_registration_confirms_the_application_by_email(client):
    response = client.post(
        "/api/auth/register/",
        data=json.dumps(REGISTRATION),
        content_type="application/json",
    )
    assert response.status_code == 201

    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.to == ["owner@sunrise.test"]
    assert "Sunrise Maternity" in sent.subject
    # It must set the expectation that sign-in is not yet possible.
    assert "not be able to sign in" in sent.body


def test_approval_notifies_the_owner(make_hospital):
    hospital = make_hospital("Notify Approved", status=Organization.STATUS_PENDING)
    mail.outbox.clear()

    hospital.org.set_review_status(Organization.STATUS_APPROVED)

    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [hospital.admin.email]
    assert "approved" in mail.outbox[0].subject.lower()


def test_rejection_notifies_the_owner_and_carries_the_note(make_hospital):
    hospital = make_hospital("Notify Rejected", status=Organization.STATUS_PENDING)
    mail.outbox.clear()

    hospital.org.set_review_status(
        Organization.STATUS_REJECTED,
        note="Licence number not found on the IHRA register.",
    )

    assert len(mail.outbox) == 1
    assert "not approved" in mail.outbox[0].subject.lower()
    assert "IHRA register" in mail.outbox[0].body


def test_re_running_a_decision_does_not_notify_twice(make_hospital):
    """Selecting Approve on an already-approved hospital must not re-email."""
    hospital = make_hospital("Idempotent", status=Organization.STATUS_PENDING)
    hospital.org.set_review_status(Organization.STATUS_APPROVED)
    mail.outbox.clear()

    hospital.org.set_review_status(Organization.STATUS_APPROVED)

    assert mail.outbox == []


def test_a_broken_mail_server_does_not_break_approval(make_hospital, settings, monkeypatch):
    """Mail is best-effort: an SMTP outage must not roll back a decision."""
    hospital = make_hospital("Mail Down", status=Organization.STATUS_PENDING)

    def explode(*args, **kwargs):
        raise OSError("SMTP unavailable")

    monkeypatch.setattr("momcare_platform.core.common.mail.send_mail", explode)

    hospital.org.set_review_status(Organization.STATUS_APPROVED)

    hospital.org.refresh_from_db()
    assert hospital.org.status == Organization.STATUS_APPROVED
    assert hospital.org.can_authenticate
