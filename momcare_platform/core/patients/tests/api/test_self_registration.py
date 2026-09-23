"""Patient self-registration and the hospital join request.

The rule these tests hold: a woman who registers herself belongs to no
hospital and has no clinical record until one approves her. Approval is the
only thing that creates a Patient, and it goes through the same
``onboard_patient`` a walk-in does.
"""

import json
import re

import pytest
from django.conf import settings
from django.core import mail

from momcare_platform.core.organization.models import Organization
from momcare_platform.core.patients.models import Patient, PatientJoinRequest
from momcare_platform.core.users.models import User

pytestmark = pytest.mark.django_db

REGISTER = "/api/auth/patient/register/"
VERIFY_EMAIL = "/api/auth/patient/verify-email/"
HOSPITALS = "/api/hospitals/"
MY_REQUESTS = "/api/my-requests/"
REVIEW = "/api/patient-requests/"

DRAFT = {
    "first_name": "Ayesha",
    "last_name": "Bibi",
    "phone": "03001234567",
    "cnic": "61101-7654321-0",
    "blood_group": "O+",
    "consent_date": "2026-02-10",
    "lmp": "2026-02-01",
    "gravida": 2,
    "para": 1,
    "previous_c_section": "yes",
}


def post(client, url, body, headers=None):
    return client.post(url, data=json.dumps(body), content_type="application/json", **(headers or {}))


def code_from_email(message):
    """Pull the six-digit OTP out of the emailed body, the way she'd read it
    off her own phone."""
    found = re.search(r"\b(\d{6})\b", message.body)
    assert found, "no OTP in the email"
    return found.group(1)


@pytest.fixture
def registered_patient(client):
    """A self-registered, email-verified woman, and her auth headers.

    Goes through the real two-step flow (register, then confirm the OTP
    emailed to her) rather than minting a token directly — the same
    discipline the ``auth`` fixture in conftest.py already applies to
    hospital logins.
    """

    def _make(email="ayesha@example.test"):
        response = post(
            client,
            REGISTER,
            {
                "email": email,
                "password": "HerOwnPick!2026",
                "first_name": "Ayesha",
                "last_name": "Bibi",
            },
        )
        assert response.status_code == 201, response.content
        code = code_from_email(mail.outbox[-1])

        verified = post(client, VERIFY_EMAIL, {"email": email, "code": code})
        assert verified.status_code == 200, verified.content
        token = verified.json()["access"]
        return User.objects.get(email=email), {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    return _make


# ── Registration ─────────────────────────────────────────────────────────────


def test_she_can_register_herself_with_no_hospital(client):
    response = post(
        client,
        REGISTER,
        {
            "email": "selfreg@example.test",
            "password": "HerOwnPick!2026",
            "first_name": "Ayesha",
        },
    )

    assert response.status_code == 201
    user = User.objects.get(email="selfreg@example.test")
    assert user.organization is None, "she belongs to no hospital yet"
    assert user.role_code == settings.ROLE_PATIENT
    assert not Patient.objects.exists(), "no clinical record until a hospital approves her"


def test_registering_alone_does_not_sign_her_in(client):
    """No tokens until she confirms the OTP — see VerifyPatientEmailView."""
    response = post(
        client,
        REGISTER,
        {"email": "notyet@example.test", "password": "HerOwnPick!2026", "first_name": "Ayesha"},
    )

    assert response.status_code == 201
    assert "access" not in response.json()
    user = User.objects.get(email="notyet@example.test")
    assert user.is_email_verified is False


def test_after_verifying_her_email_she_is_signed_in(client, registered_patient):
    _, headers = registered_patient()

    response = client.get("/api/auth/me/", **headers)

    assert response.status_code == 200
    assert response.json()["organization_id"] is None


def test_she_can_sign_in_afterwards(client, registered_patient):
    registered_patient(email="returning@example.test")

    response = post(
        client,
        "/api/auth/login/",
        {
            "email": "returning@example.test",
            "password": "HerOwnPick!2026",
        },
    )

    assert response.status_code == 200, "an org-less account must not be gated"


def test_a_duplicate_email_is_rejected(client, make_hospital, registered_patient):
    hospital = make_hospital("Dup Email Hospital")

    response = post(
        client,
        REGISTER,
        {
            "email": hospital.admin.email,
            "password": "HerOwnPick!2026",
            "first_name": "Impostor",
        },
    )

    assert response.status_code == 400
    assert "email" in response.json()


def test_a_weak_password_is_rejected(client):
    response = post(
        client,
        REGISTER,
        {
            "email": "weak@example.test",
            "password": "12345678",
            "first_name": "Ayesha",
        },
    )

    assert response.status_code == 400


# ── Hospital directory ───────────────────────────────────────────────────────


def test_she_sees_only_approved_hospitals(client, make_hospital, registered_patient):
    make_hospital("Approved Hospital")
    make_hospital("Pending Hospital", status=Organization.STATUS_PENDING)
    _, headers = registered_patient()

    body = client.get(HOSPITALS, **headers).json()

    names = [h["name"] for h in body["results"]]
    assert "Approved Hospital" in names
    assert "Pending Hospital" not in names


def test_the_directory_is_searchable(client, make_hospital, registered_patient):
    make_hospital("Sunrise Maternity")
    make_hospital("Riverside Clinic")
    _, headers = registered_patient()

    body = client.get(f"{HOSPITALS}?search=Sunrise", **headers).json()

    assert [h["name"] for h in body["results"]] == ["Sunrise Maternity"]


def test_the_directory_never_leaks_internal_counts(client, make_hospital, registered_patient):
    make_hospital("Private Hospital")
    _, headers = registered_patient()

    row = client.get(HOSPITALS, **headers).json()["results"][0]

    for leaked in ("patient_count", "staff_count", "license_number", "status"):
        assert leaked not in row, f"{leaked} must not be public"


# ── Sending a request ────────────────────────────────────────────────────────


def test_she_can_request_to_join_a_hospital(client, make_hospital, registered_patient):
    hospital = make_hospital("Target Hospital")
    user, headers = registered_patient()

    response = post(client, MY_REQUESTS, {"organization": str(hospital.org.id), "draft": DRAFT}, headers)

    assert response.status_code == 201, response.content
    body = response.json()
    assert body["status"] == "pending"
    assert body["organization_name"] == "Target Hospital"
    assert not Patient.objects.exists(), "still no clinical record while pending"


def test_a_second_request_to_the_same_hospital_is_refused(client, make_hospital, registered_patient):
    hospital = make_hospital("Repeat Hospital")
    _, headers = registered_patient()
    post(client, MY_REQUESTS, {"organization": str(hospital.org.id), "draft": DRAFT}, headers)

    response = post(client, MY_REQUESTS, {"organization": str(hospital.org.id), "draft": DRAFT}, headers)

    assert response.status_code == 409


def test_she_cannot_request_an_unapproved_hospital(client, make_hospital, registered_patient):
    hospital = make_hospital("Unapproved Hospital", status=Organization.STATUS_PENDING)
    _, headers = registered_patient()

    response = post(client, MY_REQUESTS, {"organization": str(hospital.org.id), "draft": DRAFT}, headers)

    assert response.status_code == 404, "a hospital under review is not hers to discover"


def test_an_invalid_draft_is_rejected_up_front(client, make_hospital, registered_patient):
    hospital = make_hospital("Bad Draft Hospital")
    _, headers = registered_patient()

    response = post(
        client,
        MY_REQUESTS,
        {"organization": str(hospital.org.id), "draft": {"last_name": "NoFirstName"}},
        headers,
    )

    assert response.status_code == 400
    assert "first_name" in response.json()


def test_she_sees_only_her_own_requests(client, make_hospital, registered_patient):
    hospital = make_hospital("Shared Hospital")
    _, mine = registered_patient(email="mine@example.test")
    _, hers = registered_patient(email="hers@example.test")
    post(client, MY_REQUESTS, {"organization": str(hospital.org.id), "draft": DRAFT}, mine)

    body = client.get(MY_REQUESTS, **hers).json()

    assert body["count"] == 0, "another woman's request must be invisible"


# ── The hospital reviewing ───────────────────────────────────────────────────


def test_the_hospital_sees_requests_addressed_to_it(client, make_hospital, registered_patient, auth):
    hospital = make_hospital("Reviewing Hospital")
    _, headers = registered_patient()
    post(client, MY_REQUESTS, {"organization": str(hospital.org.id), "draft": DRAFT}, headers)

    body = client.get(REVIEW, **auth(hospital.admin.email)).json()

    assert body["count"] == 1
    assert body["results"][0]["applicant_email"] == "ayesha@example.test"


def test_another_hospital_never_sees_that_queue(client, make_hospital, registered_patient, auth):
    hospital = make_hospital("Owner Queue Hospital")
    rival = make_hospital("Rival Queue Hospital")
    _, headers = registered_patient()
    post(client, MY_REQUESTS, {"organization": str(hospital.org.id), "draft": DRAFT}, headers)

    body = client.get(REVIEW, **auth(rival.admin.email)).json()

    assert body["count"] == 0


def test_approving_creates_the_patient_through_normal_onboarding(
    client,
    make_hospital,
    registered_patient,
    auth,
):
    """The whole design: approval is not a second creation path."""
    hospital = make_hospital("Approving Hospital")
    user, headers = registered_patient()
    request_id = post(client, MY_REQUESTS, {"organization": str(hospital.org.id), "draft": DRAFT}, headers).json()[
        "id"
    ]

    response = post(client, f"{REVIEW}{request_id}/approve/", {}, auth(hospital.admin.email))

    assert response.status_code == 200, response.content
    patient = Patient.objects.get()
    assert patient.organization == hospital.org
    assert patient.location is not None, "placed at the hospital's location"
    assert patient.user == user, "her login is linked to the record"
    assert patient.first_name == "Ayesha"
    assert patient.consent_date is not None
    pregnancy = patient.current_pregnancy
    assert pregnancy is not None, "her reported LMP opened a pregnancy"
    assert pregnancy.previous_c_section == "yes", "her obstetric history carried across"


def test_approving_marks_the_request_and_links_the_patient(client, make_hospital, registered_patient, auth):
    hospital = make_hospital("Marked Hospital")
    _, headers = registered_patient()
    request_id = post(client, MY_REQUESTS, {"organization": str(hospital.org.id), "draft": DRAFT}, headers).json()[
        "id"
    ]

    post(client, f"{REVIEW}{request_id}/approve/", {}, auth(hospital.admin.email))

    join_request = PatientJoinRequest.objects.get(id=request_id)
    assert join_request.status == PatientJoinRequest.STATUS_APPROVED
    assert join_request.patient is not None
    assert join_request.decided_by == hospital.admin
    assert join_request.decided_at is not None


def test_rejecting_leaves_her_account_and_draft_intact(client, make_hospital, registered_patient, auth):
    """She can go and ask a different hospital."""
    hospital = make_hospital("Rejecting Hospital")
    user, headers = registered_patient()
    request_id = post(client, MY_REQUESTS, {"organization": str(hospital.org.id), "draft": DRAFT}, headers).json()[
        "id"
    ]

    response = post(
        client,
        f"{REVIEW}{request_id}/reject/",
        {"note": "Outside our catchment area."},
        auth(hospital.admin.email),
    )

    assert response.status_code == 200
    join_request = PatientJoinRequest.objects.get(id=request_id)
    assert join_request.status == PatientJoinRequest.STATUS_REJECTED
    assert join_request.decision_note == "Outside our catchment area."
    assert not Patient.objects.exists()
    user.refresh_from_db()
    assert user.is_active is True, "her account survives a rejection"


def test_she_can_ask_another_hospital_after_a_rejection(client, make_hospital, registered_patient, auth):
    first = make_hospital("First Choice Hospital")
    second = make_hospital("Second Choice Hospital")
    _, headers = registered_patient()
    request_id = post(client, MY_REQUESTS, {"organization": str(first.org.id), "draft": DRAFT}, headers).json()["id"]
    post(client, f"{REVIEW}{request_id}/reject/", {}, auth(first.admin.email))

    response = post(client, MY_REQUESTS, {"organization": str(second.org.id), "draft": DRAFT}, headers)

    assert response.status_code == 201


def test_deciding_twice_is_refused(client, make_hospital, registered_patient, auth):
    hospital = make_hospital("Double Decide Hospital")
    _, headers = registered_patient()
    request_id = post(client, MY_REQUESTS, {"organization": str(hospital.org.id), "draft": DRAFT}, headers).json()[
        "id"
    ]
    post(client, f"{REVIEW}{request_id}/approve/", {}, auth(hospital.admin.email))

    response = post(client, f"{REVIEW}{request_id}/reject/", {}, auth(hospital.admin.email))

    assert response.status_code == 409


def test_another_hospital_cannot_approve_our_request(client, make_hospital, registered_patient, auth):
    hospital = make_hospital("Owner Approve Hospital")
    rival = make_hospital("Rival Approve Hospital")
    _, headers = registered_patient()
    request_id = post(client, MY_REQUESTS, {"organization": str(hospital.org.id), "draft": DRAFT}, headers).json()[
        "id"
    ]

    response = post(client, f"{REVIEW}{request_id}/approve/", {}, auth(rival.admin.email))

    assert response.status_code == 404
    assert not Patient.objects.exists()


def test_a_patient_cannot_reach_the_review_queue(client, make_hospital, registered_patient):
    make_hospital("No Peeking Hospital")
    _, headers = registered_patient()

    response = client.get(REVIEW, **headers)

    assert response.status_code == 403, "the review queue is hospital staff only"


# ── Asking several hospitals at once ─────────────────────────────────────────


def test_she_can_ask_several_hospitals_at_once(client, make_hospital, registered_patient):
    """The pending-request constraint is per hospital, not overall."""
    alpha = make_hospital("Alpha Choice Hospital")
    beta = make_hospital("Beta Choice Hospital")
    _, headers = registered_patient()

    first = post(client, MY_REQUESTS, {"organization": str(alpha.org.id), "draft": DRAFT}, headers)
    second = post(client, MY_REQUESTS, {"organization": str(beta.org.id), "draft": DRAFT}, headers)

    assert first.status_code == 201
    assert second.status_code == 201


def test_the_first_hospital_to_approve_gets_her(client, make_hospital, registered_patient, auth):
    alpha = make_hospital("Alpha Wins Hospital")
    beta = make_hospital("Beta Loses Hospital")
    _, headers = registered_patient()
    req_a = post(client, MY_REQUESTS, {"organization": str(alpha.org.id), "draft": DRAFT}, headers).json()["id"]
    req_b = post(client, MY_REQUESTS, {"organization": str(beta.org.id), "draft": DRAFT}, headers).json()["id"]

    post(client, f"{REVIEW}{req_a}/approve/", {}, auth(alpha.admin.email))

    assert PatientJoinRequest.objects.get(id=req_a).status == PatientJoinRequest.STATUS_APPROVED
    other = PatientJoinRequest.objects.get(id=req_b)
    assert other.status == PatientJoinRequest.STATUS_WITHDRAWN, "her other request must close when someone accepts her"
    assert other.decided_by is None, "no hospital made that decision, so none is credited"
    assert Patient.objects.count() == 1


def test_a_second_hospital_approving_is_409_not_a_crash(client, make_hospital, registered_patient, auth):
    """Regression: Patient.user is a one-to-one, so a second approval used to
    raise IntegrityError and surface as a 500."""
    alpha = make_hospital("Alpha First Hospital")
    beta = make_hospital("Beta Second Hospital")
    _, headers = registered_patient()
    req_a = post(client, MY_REQUESTS, {"organization": str(alpha.org.id), "draft": DRAFT}, headers).json()["id"]
    req_b = post(client, MY_REQUESTS, {"organization": str(beta.org.id), "draft": DRAFT}, headers).json()["id"]
    post(client, f"{REVIEW}{req_a}/approve/", {}, auth(alpha.admin.email))
    # Force it back to pending so the already-decided guard is not what answers.
    PatientJoinRequest.objects.filter(id=req_b).update(status=PatientJoinRequest.STATUS_PENDING)

    response = post(client, f"{REVIEW}{req_b}/approve/", {}, auth(beta.admin.email))

    assert response.status_code == 409
    assert "another hospital" in response.json()["detail"]
    assert Patient.objects.count() == 1, "no second record, and no crash"


def test_she_sees_the_withdrawal_on_her_own_list(client, make_hospital, registered_patient, auth):
    alpha = make_hospital("Alpha Visible Hospital")
    beta = make_hospital("Beta Visible Hospital")
    _, headers = registered_patient()
    req_a = post(client, MY_REQUESTS, {"organization": str(alpha.org.id), "draft": DRAFT}, headers).json()["id"]
    post(client, MY_REQUESTS, {"organization": str(beta.org.id), "draft": DRAFT}, headers)
    post(client, f"{REVIEW}{req_a}/approve/", {}, auth(alpha.admin.email))

    rows = client.get(MY_REQUESTS, **headers).json()["results"]

    statuses = sorted(r["status"] for r in rows)
    assert statuses == ["approved", "withdrawn"]


def test_a_get_on_approve_or_reject_is_a_clean_405_not_a_500(client, make_hospital, registered_patient, auth):
    """The decision endpoints are POST-only, and a wrong method must say so.

    ``JoinRequestDecisionView`` used to subclass the queue view and inherit
    its ``get``, whose signature takes no ``request_id`` — so a GET on
    ``/patient-requests/<id>/approve/`` raised TypeError and returned 500,
    an unintended endpoint that leaked a stack trace instead of refusing.
    """
    hospital = make_hospital("Method Guard Hospital")
    _, headers = registered_patient("methodguard@example.test")
    request_id = post(
        client,
        MY_REQUESTS,
        {"organization": str(hospital.org.id), "draft": DRAFT},
        headers,
    ).json()["id"]
    admin_headers = auth(hospital.admin.email)

    for action in ("approve", "reject"):
        response = client.get(f"{REVIEW}{request_id}/{action}/", **admin_headers)
        assert response.status_code == 405, f"GET on {action} returned {response.status_code}"


# ── Withdrawing ──────────────────────────────────────────────────────────────


def withdraw_url(request_id):
    return f"{MY_REQUESTS}{request_id}/withdraw/"


def test_she_can_withdraw_a_pending_request(client, make_hospital, registered_patient):
    hospital = make_hospital("Withdraw Hospital")
    _, headers = registered_patient("withdraw@example.test")
    request_id = post(client, MY_REQUESTS, {"organization": str(hospital.org.id), "draft": DRAFT}, headers).json()[
        "id"
    ]

    response = post(client, withdraw_url(request_id), {}, headers)

    assert response.status_code == 200, response.content
    assert response.json()["status"] == PatientJoinRequest.STATUS_WITHDRAWN
    assert PatientJoinRequest.objects.get(id=request_id).status == PatientJoinRequest.STATUS_WITHDRAWN


def test_withdrawing_frees_her_to_apply_to_the_same_hospital_again(client, make_hospital, registered_patient):
    """The one-pending-request-per-hospital constraint counts only pending
    ones, so withdrawing must genuinely release the slot — otherwise a
    mistaken send locks her out of that hospital permanently."""
    hospital = make_hospital("Reapply Hospital")
    _, headers = registered_patient("reapply@example.test")
    body = {"organization": str(hospital.org.id), "draft": DRAFT}
    first_id = post(client, MY_REQUESTS, body, headers).json()["id"]
    post(client, withdraw_url(first_id), {}, headers)

    again = post(client, MY_REQUESTS, body, headers)

    assert again.status_code == 201, again.content


def test_she_cannot_withdraw_someone_elses_request(client, make_hospital, registered_patient):
    """Scoped to her own rows before the lookup, so another woman's request
    is 404 — a 403 would confirm it exists."""
    hospital = make_hospital("Not Hers Hospital")
    _, hers = registered_patient("mine@example.test")
    _, theirs = registered_patient("other@example.test")
    other_id = post(client, MY_REQUESTS, {"organization": str(hospital.org.id), "draft": DRAFT}, theirs).json()["id"]

    response = post(client, withdraw_url(other_id), {}, hers)

    assert response.status_code == 404
    assert PatientJoinRequest.objects.get(id=other_id).status == PatientJoinRequest.STATUS_PENDING


def test_she_cannot_withdraw_after_a_hospital_approved_her(client, make_hospital, registered_patient, auth):
    """Approval created a real Patient and a care relationship. Undoing that
    is a discharge, not a POST from the phone."""
    hospital = make_hospital("Already Approved Hospital")
    _, headers = registered_patient("approved@example.test")
    request_id = post(client, MY_REQUESTS, {"organization": str(hospital.org.id), "draft": DRAFT}, headers).json()[
        "id"
    ]
    approved = post(client, f"{REVIEW}{request_id}/approve/", {}, auth(hospital.admin.email))
    assert approved.status_code == 200, approved.content

    response = post(client, withdraw_url(request_id), {}, headers)

    assert response.status_code == 409, response.content
    assert PatientJoinRequest.objects.get(id=request_id).status == PatientJoinRequest.STATUS_APPROVED


def test_she_cannot_withdraw_a_rejected_request(client, make_hospital, registered_patient, auth):
    """Withdrawing a rejection would rewrite the hospital's record of a
    decision it actually made."""
    hospital = make_hospital("Was Rejected Hospital")
    _, headers = registered_patient("rejected@example.test")
    request_id = post(client, MY_REQUESTS, {"organization": str(hospital.org.id), "draft": DRAFT}, headers).json()[
        "id"
    ]
    post(client, f"{REVIEW}{request_id}/reject/", {}, auth(hospital.admin.email))

    response = post(client, withdraw_url(request_id), {}, headers)

    assert response.status_code == 409, response.content
    assert PatientJoinRequest.objects.get(id=request_id).status == PatientJoinRequest.STATUS_REJECTED


def test_withdrawing_twice_is_refused_not_silently_repeated(client, make_hospital, registered_patient):
    hospital = make_hospital("Twice Hospital")
    _, headers = registered_patient("twice@example.test")
    request_id = post(client, MY_REQUESTS, {"organization": str(hospital.org.id), "draft": DRAFT}, headers).json()[
        "id"
    ]
    post(client, withdraw_url(request_id), {}, headers)

    response = post(client, withdraw_url(request_id), {}, headers)

    assert response.status_code == 409, response.content


def test_hospital_staff_cannot_withdraw_on_her_behalf(client, make_hospital, registered_patient, auth):
    """Withdraw is hers alone — a hospital declining someone is a rejection,
    which is recorded as such with who decided it."""
    hospital = make_hospital("Staff Cannot Hospital")
    _, headers = registered_patient("staffcannot@example.test")
    request_id = post(client, MY_REQUESTS, {"organization": str(hospital.org.id), "draft": DRAFT}, headers).json()[
        "id"
    ]

    response = post(client, withdraw_url(request_id), {}, auth(hospital.admin.email))

    assert response.status_code == 403


def test_there_is_no_way_to_edit_a_submitted_request(client, make_hospital, registered_patient):
    """The draft is what staff read when deciding, so it must not change
    under them. Withdraw and resend instead."""
    hospital = make_hospital("No Edit Hospital")
    _, headers = registered_patient("noedit@example.test")
    request_id = post(client, MY_REQUESTS, {"organization": str(hospital.org.id), "draft": DRAFT}, headers).json()[
        "id"
    ]

    detail = f"{MY_REQUESTS}{request_id}/"
    for method in (client.patch, client.put, client.delete):
        response = method(
            detail, data=json.dumps({"draft": {"first_name": "Changed"}}), content_type="application/json", **headers
        )
        assert response.status_code in (404, 405), f"{method.__name__} returned {response.status_code}"
