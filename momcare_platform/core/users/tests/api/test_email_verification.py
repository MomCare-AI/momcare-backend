"""Patient self-registration's email OTP — the gate between "account
created" and "account usable".

Only patient self-registration goes through this. Hospital owners are
identity-checked by a human reviewer; staff are invited to a specific
address by an admin. Neither ever gets ``is_email_verified=True`` set —
this file also proves the login gate stays scoped to patients and doesn't
lock either of them out.
"""

import json
import re
from datetime import timedelta

import pytest
from django.conf import settings
from django.core import mail
from django.utils import timezone

from momcare_platform.core.users.models import EmailVerificationCode, User

pytestmark = pytest.mark.django_db

REGISTER = "/api/auth/patient/register/"
VERIFY = "/api/auth/patient/verify-email/"
RESEND = "/api/auth/patient/resend-verification/"
LOGIN = "/api/auth/login/"

PASSWORD = "HerOwnPick!2026"


def post(client, url, **body):
    return client.post(url, data=json.dumps(body), content_type="application/json")


def code_from_email(message):
    found = re.search(r"\b(\d{6})\b", message.body)
    assert found, "no OTP in the email"
    return found.group(1)


def register(client, email="ayesha@example.test"):
    response = post(client, REGISTER, email=email, password=PASSWORD, first_name="Ayesha")
    assert response.status_code == 201, response.content
    return code_from_email(mail.outbox[-1])


# ── The full flow ─────────────────────────────────────────────────────────────


def test_the_full_flow(client):
    code = register(client)

    response = post(client, VERIFY, email="ayesha@example.test", code=code)

    assert response.status_code == 200, response.content
    assert "access" in response.json()
    user = User.objects.get(email="ayesha@example.test")
    assert user.is_email_verified is True


def test_the_issued_token_actually_works(client):
    code = register(client)
    token = post(client, VERIFY, email="ayesha@example.test", code=code).json()["access"]

    response = client.get("/api/auth/me/", HTTP_AUTHORIZATION=f"Bearer {token}")

    assert response.status_code == 200
    assert response.json()["email"] == "ayesha@example.test"


# ── Wrong / expired / exhausted codes ───────────────────────────────────────


def test_a_wrong_code_is_rejected(client):
    register(client)

    response = post(client, VERIFY, email="ayesha@example.test", code="000000")

    assert response.status_code == 400
    assert User.objects.get(email="ayesha@example.test").is_email_verified is False


def test_the_same_code_cannot_be_used_twice(client):
    code = register(client)
    post(client, VERIFY, email="ayesha@example.test", code=code)

    second = post(client, VERIFY, email="ayesha@example.test", code=code)

    assert second.status_code == 400


def test_an_expired_code_is_rejected(client):
    code = register(client)
    row = EmailVerificationCode.objects.get(user__email="ayesha@example.test", consumed_at__isnull=True)
    row.expires_at = timezone.now() - timedelta(minutes=1)
    row.save(update_fields=["expires_at"])

    response = post(client, VERIFY, email="ayesha@example.test", code=code)

    assert response.status_code == 400


def test_too_many_wrong_attempts_exhausts_the_code(client):
    """Even the RIGHT code stops working once the attempt budget is spent —
    otherwise the cap could be walked around by always guessing wrong until
    the last try, then guessing right."""
    code = register(client)

    for _ in range(EmailVerificationCode.MAX_ATTEMPTS):
        post(client, VERIFY, email="ayesha@example.test", code="000000")

    response = post(client, VERIFY, email="ayesha@example.test", code=code)

    assert response.status_code == 400
    assert User.objects.get(email="ayesha@example.test").is_email_verified is False


def test_a_missing_email_or_code_is_a_400(client):
    assert post(client, VERIFY, code="123456").status_code == 400
    assert post(client, VERIFY, email="ayesha@example.test").status_code == 400


# ── Resend ───────────────────────────────────────────────────────────────────


def test_resend_issues_a_fresh_code_and_invalidates_the_old_one(client):
    old_code = register(client)

    resend = post(client, RESEND, email="ayesha@example.test")
    assert resend.status_code == 200
    new_code = code_from_email(mail.outbox[-1])
    assert new_code != old_code

    stale = post(client, VERIFY, email="ayesha@example.test", code=old_code)
    assert stale.status_code == 400

    fresh = post(client, VERIFY, email="ayesha@example.test", code=new_code)
    assert fresh.status_code == 200


def test_resend_for_an_unknown_email_still_says_ok(client):
    """Same shape either way — confirming "no pending signup exists" is
    itself information a stranger should not get from this endpoint."""
    known = post(client, RESEND, email="nobody@example.test")

    assert known.status_code == 200
    assert "sent" in known.json()["detail"].lower() or "pending" in known.json()["detail"].lower()


def test_resend_for_an_already_verified_email_does_not_send_anything(client):
    code = register(client)
    post(client, VERIFY, email="ayesha@example.test", code=code)
    mail.outbox.clear()

    post(client, RESEND, email="ayesha@example.test")

    assert mail.outbox == []


# ── Retrying registration on an abandoned attempt ───────────────────────────


def test_registering_again_with_the_same_unverified_email_replaces_the_old_attempt(client):
    old_code = register(client, email="retry@example.test")

    new_response = post(
        client,
        REGISTER,
        email="retry@example.test",
        password=PASSWORD,
        first_name="Ayesha Again",
    )
    assert new_response.status_code == 201
    new_code = code_from_email(mail.outbox[-1])

    stale = post(client, VERIFY, email="retry@example.test", code=old_code)
    assert stale.status_code == 400

    fresh = post(client, VERIFY, email="retry@example.test", code=new_code)
    assert fresh.status_code == 200
    assert User.objects.filter(email="retry@example.test").count() == 1


def test_registering_again_with_an_already_verified_email_is_still_blocked(client):
    code = register(client, email="taken@example.test")
    post(client, VERIFY, email="taken@example.test", code=code)

    response = post(client, REGISTER, email="taken@example.test", password=PASSWORD, first_name="Impostor")

    assert response.status_code == 400


def test_retrying_does_not_delete_a_hospital_admins_account(client, make_hospital):
    """The trap this guards against: an admin's email also has
    is_email_verified=False (nothing ever sets it for that role), so without
    the role filter a 'retry' would try to delete a real hospital account —
    and 500 the moment that admin owns an Organization (a protected FK)."""
    hospital = make_hospital("Retry Trap Hospital")

    response = post(
        client,
        REGISTER,
        email=hospital.admin.email,
        password=PASSWORD,
        first_name="Impostor",
    )

    assert response.status_code == 400, response.content
    assert User.objects.filter(email=hospital.admin.email).exists()


# ── The login gate ───────────────────────────────────────────────────────────


def test_an_unverified_patient_cannot_log_in(client):
    register(client)

    response = post(client, LOGIN, email="ayesha@example.test", password=PASSWORD)

    assert response.status_code == 403
    assert response.json()["requires_email_verification"] is True


def test_a_verified_patient_can_log_in_normally(client):
    code = register(client)
    post(client, VERIFY, email="ayesha@example.test", code=code)

    response = post(client, LOGIN, email="ayesha@example.test", password=PASSWORD)

    assert response.status_code == 200


def test_a_hospital_admin_login_is_unaffected_by_the_verification_gate(client, make_hospital):
    hospital = make_hospital("Unaffected Hospital")

    response = post(client, LOGIN, email=hospital.admin.email, password=hospital.password)

    assert response.status_code == 200, response.content


def test_a_staff_login_is_unaffected_by_the_verification_gate(client, make_hospital, make_staff):
    hospital = make_hospital("Unaffected Staff Hospital")
    make_staff(hospital.org, settings.ROLE_NURSE, "n@unaffected.test")

    response = post(client, LOGIN, email="n@unaffected.test", password="TestPass!2026")

    assert response.status_code == 200, response.content


# ── This endpoint is patient-only, even though every other role also sits at
# is_email_verified=False forever (nothing ever sets it for them) ──────────


def test_verify_cannot_be_used_to_log_in_as_a_hospital_admin(client, make_hospital):
    """The bypass this guards against: since a hospital_admin's row also has
    is_email_verified=False permanently, submitting a *correct* code for
    their email must still be refused — not silently issue that admin's
    real tokens to whoever holds the code. Proves the role filter is what
    stops it, not the code check (the code here is genuinely valid)."""
    hospital = make_hospital("Verify Bypass Trap Hospital")
    _, code = EmailVerificationCode.issue(hospital.admin)

    response = post(client, VERIFY, email=hospital.admin.email, code=code)

    assert response.status_code == 400, response.content
    assert "access" not in response.json()
    hospital.admin.refresh_from_db()
    assert hospital.admin.is_email_verified is False


def test_verify_cannot_be_used_to_log_in_as_a_staff_member(client, make_hospital, make_staff):
    hospital = make_hospital("Verify Staff Bypass Trap Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "n@verifybypass.test")
    _, code = EmailVerificationCode.issue(nurse)

    response = post(client, VERIFY, email=nurse.email, code=code)

    assert response.status_code == 400, response.content
    assert "access" not in response.json()


def test_resend_does_not_email_a_hospital_admin_a_patient_otp(client, make_hospital):
    hospital = make_hospital("Resend Bypass Trap Hospital")
    mail.outbox.clear()

    response = post(client, RESEND, email=hospital.admin.email)

    assert response.status_code == 200, response.content
    assert len(mail.outbox) == 0
    assert not EmailVerificationCode.objects.filter(user=hospital.admin).exists()
