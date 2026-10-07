"""Patient self-registration and the hospital join request.

The rules these tests hold:

* A woman who registers herself belongs to no hospital and has no clinical
  record until a hospital onboards her.
* She fills in her profile once. A join request carries only the hospital (and
  optionally one of its branches); the server freezes a copy of her profile
  onto it.
* A hospital opens the request in its normal onboarding form, pre-filled from
  that copy, and saving the form IS the approval: ``POST /patients/`` with the
  request's id, through the same ``onboard_patient`` a walk-in uses.
"""

import json
from datetime import date, timedelta

import pytest
from django.conf import settings

from momcare_platform.core.locations.models import Location
from momcare_platform.core.organization.models import Organization
from momcare_platform.core.patients.models import Patient, PatientJoinRequest
from momcare_platform.core.patients.tests.conftest import MY_PROFILE, patch_json, post_json
from momcare_platform.core.users.models import User

pytestmark = pytest.mark.django_db

REGISTER = "/api/auth/patient/register/"
HOSPITALS = "/api/hospitals/"
MY_REQUESTS = "/api/my-requests/"
REVIEW = "/api/patient-requests/"
PATIENTS = "/api/patients/"


def send(client, hospital, headers, **extra):
    """She sends a request: the hospital, nothing else."""
    return post_json(client, MY_REQUESTS, {"organization": str(hospital.org.id), **extra}, headers)


@pytest.fixture
def ready_patient(registered_patient, complete_profile):
    """A verified woman whose profile is complete, so she may send requests."""

    def _make(email="ayesha@example.test"):
        user, headers = registered_patient(email)
        complete_profile(headers)
        return user, headers

    return _make


def add_branch(hospital, name="E-9 Branch"):
    return Location.objects.create(organization=hospital.org, name=name, location_manager=hospital.admin)


def onboard(client, staff_headers, request_id, **overrides):
    """What the web app does: open the request, take its snapshot as the
    pre-filled form, and save it with the request's id."""
    detail = client.get(f"{REVIEW}{request_id}/", **staff_headers).json()
    form = {**detail["profile"], **overrides, "join_request": request_id}
    return post_json(client, PATIENTS, form, staff_headers)


# -- Registration -----------------------------------------------------------------


def test_she_can_register_herself_with_no_hospital(client):
    response = post_json(
        client,
        REGISTER,
        {
            "email": "selfreg@example.test",
            "password": "HerOwnPick!2026",
            "first_name": "Ayesha",
            "last_name": "Bibi",
            "address_line1": "House 12, Street 4",
            "address_line2": "F-7",
            "city": "Islamabad",
            "state": "ICT",
            "postal_code": "44000",
            "country": "Pakistan",
        },
    )

    assert response.status_code == 201
    user = User.objects.get(email="selfreg@example.test")
    assert user.organization is None, "she belongs to no hospital yet"
    assert user.role_code == settings.ROLE_PATIENT
    assert not Patient.objects.exists(), "no clinical record until a hospital onboards her"


def test_registering_alone_does_not_sign_her_in(client):
    """No tokens until she confirms the OTP -- see VerifyPatientEmailView."""
    response = post_json(
        client,
        REGISTER,
        {
            "email": "notyet@example.test",
            "password": "HerOwnPick!2026",
            "first_name": "Ayesha",
            "last_name": "Bibi",
            "address_line1": "House 12, Street 4",
            "address_line2": "F-7",
            "city": "Islamabad",
            "state": "ICT",
            "postal_code": "44000",
            "country": "Pakistan",
        },
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

    response = post_json(
        client,
        "/api/auth/login/",
        {"email": "returning@example.test", "password": "HerOwnPick!2026"},
    )

    assert response.status_code == 200, "an org-less account must not be gated"


def test_a_duplicate_email_is_rejected(client, make_hospital):
    hospital = make_hospital("Dup Email Hospital")

    response = post_json(
        client,
        REGISTER,
        {"email": hospital.admin.email, "password": "HerOwnPick!2026", "first_name": "Impostor", "last_name": "X"},
    )

    assert response.status_code == 400
    assert "email" in response.json()


def test_a_weak_password_is_rejected(client):
    response = post_json(
        client,
        REGISTER,
        {"email": "weak@example.test", "password": "12345678", "first_name": "Ayesha", "last_name": "Bibi"},
    )

    assert response.status_code == 400


# -- Hospital directory -----------------------------------------------------------


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


def test_the_directory_uses_the_standard_pagination_envelope(client, make_hospital, registered_patient):
    make_hospital("Envelope Directory Hospital")
    _, headers = registered_patient()

    body = client.get(HOSPITALS, **headers).json()

    assert set(body.keys()) == {"count", "page", "page_size", "total_pages", "next", "previous", "results"}
    assert body["count"] >= 1


def test_each_hospital_lists_its_active_branches(client, make_hospital, registered_patient):
    hospital = make_hospital("Branches Hospital")
    e9 = add_branch(hospital, "E-9 Branch")
    closed = add_branch(hospital, "Closed Branch")
    closed.is_active = False
    closed.save(update_fields=["is_active", "updated_at"])
    _, headers = registered_patient()

    row = client.get(f"{HOSPITALS}?search=Branches", **headers).json()["results"][0]

    assert [b["name"] for b in row["locations"]] == ["E-9 Branch"]
    assert row["locations"][0]["id"] == str(e9.id)


def test_a_hospital_with_no_branch_yet_lists_none(client, make_hospital, registered_patient):
    make_hospital("No Branch Hospital")
    _, headers = registered_patient()

    row = client.get(f"{HOSPITALS}?search=No Branch", **headers).json()["results"][0]

    assert row["locations"] == []


# -- Sending a request ------------------------------------------------------------


def test_she_cannot_send_a_request_until_her_profile_is_complete(client, make_hospital, registered_patient):
    hospital = make_hospital("Incomplete Profile Hospital")
    _, headers = registered_patient()

    response = send(client, hospital, headers)

    assert response.status_code == 400
    body = response.json()
    assert "date_of_birth" in body["missing_fields"]
    assert "emergency_contact_name" in body["missing_fields"]
    assert not PatientJoinRequest.objects.exists()


def test_she_can_request_to_join_with_only_the_hospital(client, make_hospital, ready_patient):
    hospital = make_hospital("Target Hospital")
    _, headers = ready_patient()

    response = send(client, hospital, headers)

    assert response.status_code == 201, response.content
    body = response.json()
    assert body["status"] == "pending"
    assert body["organization_name"] == "Target Hospital"
    assert body["location"] is None
    assert "draft" not in body
    assert not Patient.objects.exists(), "still no clinical record while pending"


def test_the_request_carries_a_snapshot_of_her_profile(client, make_hospital, ready_patient):
    hospital = make_hospital("Snapshot Hospital")
    _, headers = ready_patient()

    profile = send(client, hospital, headers).json()["profile"]

    assert profile["first_name"] == "Ayesha"
    assert profile["last_name"] == "Bibi"
    assert profile["date_of_birth"] == "1996-04-12"
    assert profile["national_id"] == "61101-7654321-0"
    assert profile["emergency_contact_name"] == "Bilal Ahmed"
    assert profile["city"] == "Islamabad"
    assert "gender" not in profile


def test_the_snapshot_is_built_by_the_server_not_the_client(client, make_hospital, ready_patient):
    hospital = make_hospital("Server Snapshot Hospital")
    _, headers = ready_patient()

    profile = send(client, hospital, headers, profile={"first_name": "Forged"}, draft={"first_name": "Forged"}).json()[
        "profile"
    ]

    assert profile["first_name"] == "Ayesha"


def test_editing_her_profile_later_does_not_change_a_request_already_sent(client, make_hospital, ready_patient):
    first = make_hospital("Frozen Hospital")
    second = make_hospital("Later Hospital")
    _, headers = ready_patient()
    first_id = send(client, first, headers).json()["id"]

    patch_json(client, MY_PROFILE, {"emergency_contact_name": "Changed Name"}, headers)
    later = send(client, second, headers).json()

    frozen = PatientJoinRequest.objects.get(id=first_id)
    assert frozen.draft["emergency_contact_name"] == "Bilal Ahmed", "what the hospital was shown stays"
    assert later["profile"]["emergency_contact_name"] == "Changed Name"


def test_a_second_request_to_the_same_hospital_is_refused(client, make_hospital, ready_patient):
    hospital = make_hospital("Repeat Hospital")
    _, headers = ready_patient()
    send(client, hospital, headers)

    response = send(client, hospital, headers)

    assert response.status_code == 409


def test_she_cannot_request_an_unapproved_hospital(client, make_hospital, ready_patient):
    hospital = make_hospital("Unapproved Hospital", status=Organization.STATUS_PENDING)
    _, headers = ready_patient()

    response = send(client, hospital, headers)

    assert response.status_code == 404, "a hospital under review is not hers to discover"


def test_a_request_must_name_a_hospital(client, ready_patient):
    _, headers = ready_patient()

    response = post_json(client, MY_REQUESTS, {}, headers)

    assert response.status_code == 400
    assert "organization" in response.json()


def test_she_can_ask_for_a_specific_branch(client, make_hospital, ready_patient):
    hospital = make_hospital("Branch Pick Hospital")
    e9 = add_branch(hospital)
    _, headers = ready_patient()

    body = send(client, hospital, headers, location=str(e9.id)).json()

    assert body["location"] == str(e9.id)
    assert body["location_name"] == "E-9 Branch"


def test_a_branch_of_another_hospital_is_refused(client, make_hospital, ready_patient):
    hospital = make_hospital("Own Branch Hospital")
    rival = make_hospital("Rival Branch Hospital")
    rival_branch = add_branch(rival, "Rival Branch")
    _, headers = ready_patient()

    response = send(client, hospital, headers, location=str(rival_branch.id))

    assert response.status_code == 400
    assert "location" in response.json()
    assert not PatientJoinRequest.objects.exists()


def test_a_closed_branch_is_refused(client, make_hospital, ready_patient):
    hospital = make_hospital("Closed Branch Hospital")
    branch = add_branch(hospital)
    branch.is_active = False
    branch.save(update_fields=["is_active", "updated_at"])
    _, headers = ready_patient()

    response = send(client, hospital, headers, location=str(branch.id))

    assert response.status_code == 400


def test_a_garbage_branch_id_is_a_clean_400(client, make_hospital, ready_patient):
    hospital = make_hospital("Garbage Branch Hospital")
    _, headers = ready_patient()

    response = send(client, hospital, headers, location="not-a-uuid")

    assert response.status_code == 400


def test_she_sees_only_her_own_requests(client, make_hospital, ready_patient):
    hospital = make_hospital("Shared Hospital")
    _, mine = ready_patient(email="mine@example.test")
    _, hers = ready_patient(email="hers@example.test")
    send(client, hospital, mine)

    body = client.get(MY_REQUESTS, **hers).json()

    assert body["count"] == 0, "another woman's request must be invisible"


def test_my_requests_uses_the_standard_pagination_envelope(client, make_hospital, ready_patient):
    hospital = make_hospital("My Requests Envelope Hospital")
    _, headers = ready_patient()
    send(client, hospital, headers)

    body = client.get(MY_REQUESTS, **headers).json()

    assert set(body.keys()) == {"count", "page", "page_size", "total_pages", "next", "previous", "results"}
    assert body["count"] == 1


# -- The hospital reviewing ---------------------------------------------------------


def test_the_hospital_sees_requests_addressed_to_it(client, make_hospital, ready_patient, auth):
    hospital = make_hospital("Reviewing Hospital")
    _, headers = ready_patient()
    send(client, hospital, headers)

    body = client.get(REVIEW, **auth(hospital.admin.email)).json()

    assert body["count"] == 1
    assert body["results"][0]["applicant_email"] == "ayesha@example.test"


def test_another_hospital_never_sees_that_queue(client, make_hospital, ready_patient, auth):
    hospital = make_hospital("Owner Queue Hospital")
    rival = make_hospital("Rival Queue Hospital")
    _, headers = ready_patient()
    send(client, hospital, headers)

    body = client.get(REVIEW, **auth(rival.admin.email)).json()

    assert body["count"] == 0


def test_staff_can_open_one_request_to_prefill_the_onboarding_form(client, make_hospital, ready_patient, auth):
    hospital = make_hospital("Detail Hospital")
    _, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]

    response = client.get(f"{REVIEW}{request_id}/", **auth(hospital.admin.email))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == request_id
    assert body["applicant_email"] == "ayesha@example.test"
    assert body["profile"]["first_name"] == "Ayesha"
    assert body["profile"]["national_id"] == "61101-7654321-0"


def test_another_hospital_cannot_open_our_request(client, make_hospital, ready_patient, auth):
    hospital = make_hospital("Detail Owner Hospital")
    rival = make_hospital("Detail Rival Hospital")
    _, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]

    response = client.get(f"{REVIEW}{request_id}/", **auth(rival.admin.email))

    assert response.status_code == 404


def test_a_patient_cannot_reach_the_review_queue(client, make_hospital, ready_patient):
    make_hospital("No Peeking Hospital")
    _, headers = ready_patient()

    assert client.get(REVIEW, **headers).status_code == 403, "the review queue is hospital staff only"


def test_a_patient_cannot_open_a_request_detail(client, make_hospital, ready_patient):
    hospital = make_hospital("No Peeking Detail Hospital")
    _, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]

    assert client.get(f"{REVIEW}{request_id}/", **headers).status_code == 403


def test_there_is_no_one_click_approve_any_more(client, make_hospital, ready_patient, auth):
    """Saving the onboarding form is the approval; an approve endpoint would be a
    second way to create a patient with none of the clinical fields."""
    hospital = make_hospital("No Approve Hospital")
    _, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]

    response = post_json(client, f"{REVIEW}{request_id}/approve/", {}, auth(hospital.admin.email))

    assert response.status_code == 404
    assert not Patient.objects.exists()


def test_rejecting_leaves_her_account_and_profile_intact(client, make_hospital, ready_patient, auth):
    """She can go and ask a different hospital."""
    hospital = make_hospital("Rejecting Hospital")
    user, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]

    response = post_json(
        client,
        f"{REVIEW}{request_id}/reject/",
        {"note": "Outside our catchment area."},
        auth(hospital.admin.email),
    )

    assert response.status_code == 200
    join_request = PatientJoinRequest.objects.get(id=request_id)
    assert join_request.status == PatientJoinRequest.STATUS_REJECTED
    assert join_request.decision_note == "Outside our catchment area."
    assert join_request.decided_by == hospital.admin
    assert not Patient.objects.exists()
    user.refresh_from_db()
    assert user.is_active is True, "her account survives a rejection"


def test_she_can_ask_another_hospital_after_a_rejection(client, make_hospital, ready_patient, auth):
    first = make_hospital("First Choice Hospital")
    second = make_hospital("Second Choice Hospital")
    _, headers = ready_patient()
    request_id = send(client, first, headers).json()["id"]
    post_json(client, f"{REVIEW}{request_id}/reject/", {}, auth(first.admin.email))

    assert send(client, second, headers).status_code == 201


def test_rejecting_twice_is_refused(client, make_hospital, ready_patient, auth):
    hospital = make_hospital("Double Reject Hospital")
    _, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]
    post_json(client, f"{REVIEW}{request_id}/reject/", {}, auth(hospital.admin.email))

    response = post_json(client, f"{REVIEW}{request_id}/reject/", {}, auth(hospital.admin.email))

    assert response.status_code == 409


def test_another_hospital_cannot_reject_our_request(client, make_hospital, ready_patient, auth):
    hospital = make_hospital("Owner Reject Hospital")
    rival = make_hospital("Rival Reject Hospital")
    _, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]

    response = post_json(client, f"{REVIEW}{request_id}/reject/", {}, auth(rival.admin.email))

    assert response.status_code == 404
    assert PatientJoinRequest.objects.get(id=request_id).status == PatientJoinRequest.STATUS_PENDING


def test_a_get_on_reject_is_a_clean_405_not_a_500(client, make_hospital, ready_patient, auth):
    hospital = make_hospital("Method Guard Hospital")
    _, headers = ready_patient("methodguard@example.test")
    request_id = send(client, hospital, headers).json()["id"]

    response = client.get(f"{REVIEW}{request_id}/reject/", **auth(hospital.admin.email))

    assert response.status_code == 405


# -- Saving the onboarding form is the approval ---------------------------------------


def test_saving_the_form_onboards_her_through_the_normal_path(client, make_hospital, ready_patient, auth):
    """The whole design: one creation path, so a self-registered woman and a
    walk-in are the same kind of record."""
    hospital = make_hospital("Approving Hospital")
    user, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]

    response = onboard(client, auth(hospital.admin.email), request_id)

    assert response.status_code == 201, response.content
    patient = Patient.objects.get()
    assert patient.organization == hospital.org
    assert patient.location is not None, "placed at a branch of the hospital"
    assert patient.user == user, "her login is linked to the record"
    assert patient.first_name == "Ayesha"
    assert patient.national_id == "61101-7654321-0"
    assert patient.emergency_contact_name == "Bilal Ahmed"
    assert patient.city == "Islamabad", "her address came across"
    assert patient.current_pregnancy is None, "no pregnancy until a clinician opens one"


def test_saving_the_form_closes_the_request_and_links_the_patient(client, make_hospital, ready_patient, auth):
    hospital = make_hospital("Marked Hospital")
    _, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]

    onboard(client, auth(hospital.admin.email), request_id)

    join_request = PatientJoinRequest.objects.get(id=request_id)
    assert join_request.status == PatientJoinRequest.STATUS_APPROVED
    assert join_request.patient is not None
    assert join_request.decided_by == hospital.admin
    assert join_request.decided_at is not None


def test_the_request_stays_pending_until_the_form_is_saved(client, make_hospital, ready_patient, auth):
    hospital = make_hospital("Waiting Hospital")
    _, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]

    client.get(f"{REVIEW}{request_id}/", **auth(hospital.admin.email))

    assert PatientJoinRequest.objects.get(id=request_id).status == PatientJoinRequest.STATUS_PENDING
    assert not Patient.objects.exists(), "opening a request creates nothing"


def test_staff_values_win_over_her_snapshot(client, make_hospital, ready_patient, auth):
    hospital = make_hospital("Staff Wins Hospital")
    _, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]

    onboard(
        client,
        auth(hospital.admin.email),
        request_id,
        first_name="Ayesha Corrected",
        mrn="MRN-100",
        blood_group="A+",
    )

    patient = Patient.objects.get()
    assert patient.first_name == "Ayesha Corrected"
    assert patient.mrn == "MRN-100"
    assert patient.blood_group == "A+"


def test_staff_can_open_her_pregnancy_in_the_same_save(client, make_hospital, ready_patient, auth):
    hospital = make_hospital("Pregnancy Same Save Hospital")
    _, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]
    lmp = date.today() - timedelta(days=70)

    response = onboard(
        client,
        auth(hospital.admin.email),
        request_id,
        pregnancy={"lmp": lmp.isoformat(), "gravida": 2, "para": 1, "previous_c_section": "yes"},
    )

    assert response.status_code == 201, response.content
    pregnancy = Patient.objects.get().current_pregnancy
    assert pregnancy is not None
    assert pregnancy.previous_c_section == "yes"


def test_she_lands_in_the_branch_she_asked_for(client, make_hospital, ready_patient, auth):
    hospital = make_hospital("Branch Landing Hospital")
    add_branch(hospital, "Main Wing")
    e9 = add_branch(hospital, "E-9 Branch")
    _, headers = ready_patient()
    request_id = send(client, hospital, headers, location=str(e9.id)).json()["id"]

    onboard(client, auth(hospital.admin.email), request_id)

    assert Patient.objects.get().location == e9


def test_with_no_branch_asked_she_lands_in_the_default_location(client, make_hospital, ready_patient, auth):
    hospital = make_hospital("Default Landing Hospital")
    _, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]

    onboard(client, auth(hospital.admin.email), request_id)

    assert Patient.objects.get().location.organization == hospital.org


def test_a_branch_closed_since_she_asked_falls_back_to_the_default(client, make_hospital, ready_patient, auth):
    hospital = make_hospital("Closed Since Hospital")
    default = add_branch(hospital, "Open Branch")
    gone = add_branch(hospital, "Gone Branch")
    _, headers = ready_patient()
    request_id = send(client, hospital, headers, location=str(gone.id)).json()["id"]
    gone.is_active = False
    gone.save(update_fields=["is_active", "updated_at"])

    response = onboard(client, auth(hospital.admin.email), request_id)

    assert response.status_code == 201, response.content
    assert Patient.objects.get().location == default


def test_once_onboarded_her_account_belongs_to_the_hospital(client, make_hospital, ready_patient, auth):
    """Her token is what scopes her to her own hospital's data; without an
    organization on her account she could never read her own care plan."""
    hospital = make_hospital("Account Org Hospital")
    user, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]
    onboard(client, auth(hospital.admin.email), request_id)

    login = post_json(client, "/api/auth/login/", {"email": user.email, "password": "HerOwnPick!2026"})
    me = client.get("/api/auth/me/", HTTP_AUTHORIZATION=f"Bearer {login.json()['access']}").json()

    user.refresh_from_db()
    assert user.organization == hospital.org
    assert me["organization_id"] == str(hospital.org.id)
    assert me["patient_id"] == str(Patient.objects.get().id)


def test_another_hospital_cannot_onboard_from_our_request(client, make_hospital, ready_patient, auth):
    hospital = make_hospital("Owner Onboard Hospital")
    rival = make_hospital("Rival Onboard Hospital")
    _, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]
    form = {
        "first_name": "Ayesha",
        "address_line1": "x",
        "address_line2": "x",
        "city": "x",
        "state": "x",
        "postal_code": "x",
        "country": "x",
        "join_request": request_id,
    }

    response = post_json(client, PATIENTS, form, auth(rival.admin.email))

    assert response.status_code == 404
    assert not Patient.objects.exists()
    assert PatientJoinRequest.objects.get(id=request_id).status == PatientJoinRequest.STATUS_PENDING


def test_a_made_up_request_id_is_a_clean_404(client, make_hospital, auth):
    hospital = make_hospital("Made Up Hospital")
    form = {
        "first_name": "Ayesha",
        "address_line1": "x",
        "address_line2": "x",
        "city": "x",
        "state": "x",
        "postal_code": "x",
        "country": "x",
    }

    for bogus in ("not-a-uuid", "11111111-1111-1111-1111-111111111111"):
        response = post_json(client, PATIENTS, {**form, "join_request": bogus}, auth(hospital.admin.email))
        assert response.status_code in (400, 404), response.content
    assert not Patient.objects.exists()


def test_an_already_decided_request_cannot_be_onboarded_again(client, make_hospital, ready_patient, auth):
    hospital = make_hospital("Decided Twice Hospital")
    _, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]
    staff = auth(hospital.admin.email)
    onboard(client, staff, request_id)

    response = onboard(client, staff, request_id, first_name="Again", national_id="")

    assert response.status_code == 409
    assert Patient.objects.count() == 1


def test_a_double_click_on_save_is_a_409_not_a_misleading_duplicate_error(client, make_hospital, ready_patient, auth):
    """The same form sent twice (a double-tap on Save): the second must say the
    request is already decided, not that her national ID is "already
    registered" -- which would be true only because the first save worked."""
    hospital = make_hospital("Double Click Hospital")
    _, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]
    staff = auth(hospital.admin.email)
    assert onboard(client, staff, request_id, mrn="MRN-DC-1").status_code == 201

    response = onboard(client, staff, request_id, mrn="MRN-DC-1")

    assert response.status_code == 409, response.content
    assert Patient.objects.count() == 1


def test_a_rejected_request_cannot_be_onboarded(client, make_hospital, ready_patient, auth):
    hospital = make_hospital("Rejected Then Onboard Hospital")
    _, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]
    staff = auth(hospital.admin.email)
    post_json(client, f"{REVIEW}{request_id}/reject/", {}, staff)

    response = onboard(client, staff, request_id)

    assert response.status_code == 409
    assert not Patient.objects.exists()


def test_a_failed_save_leaves_the_request_pending(client, make_hospital, ready_patient, auth):
    """All or nothing: a duplicate national ID must not leave her half-onboarded."""
    hospital = make_hospital("Atomic Hospital")
    user, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]
    staff = auth(hospital.admin.email)
    post_json(
        client,
        PATIENTS,
        {
            "first_name": "Someone",
            "national_id": "61101-7654321-0",
            "address_line1": "x",
            "address_line2": "x",
            "city": "x",
            "state": "x",
            "postal_code": "x",
            "country": "x",
        },
        staff,
    )

    response = onboard(client, staff, request_id)

    assert response.status_code == 400
    assert Patient.objects.count() == 1
    assert PatientJoinRequest.objects.get(id=request_id).status == PatientJoinRequest.STATUS_PENDING
    user.refresh_from_db()
    assert user.organization is None, "her account is untouched"


def test_a_patient_cannot_onboard_herself(client, make_hospital, ready_patient):
    hospital = make_hospital("Self Onboard Hospital")
    _, headers = ready_patient()
    request_id = send(client, hospital, headers).json()["id"]
    form = {
        "first_name": "Ayesha",
        "address_line1": "x",
        "address_line2": "x",
        "city": "x",
        "state": "x",
        "postal_code": "x",
        "country": "x",
        "join_request": request_id,
    }

    response = post_json(client, PATIENTS, form, headers)

    assert response.status_code == 403
    assert not Patient.objects.exists()


# -- Asking several hospitals at once ---------------------------------------------------


def test_she_can_ask_several_hospitals_at_once(client, make_hospital, ready_patient):
    """The pending-request constraint is per hospital, not overall."""
    alpha = make_hospital("Alpha Choice Hospital")
    beta = make_hospital("Beta Choice Hospital")
    _, headers = ready_patient()

    assert send(client, alpha, headers).status_code == 201
    assert send(client, beta, headers).status_code == 201


def test_the_first_hospital_to_onboard_her_gets_her(client, make_hospital, ready_patient, auth):
    alpha = make_hospital("Alpha Wins Hospital")
    beta = make_hospital("Beta Loses Hospital")
    _, headers = ready_patient()
    req_a = send(client, alpha, headers).json()["id"]
    req_b = send(client, beta, headers).json()["id"]

    onboard(client, auth(alpha.admin.email), req_a)

    assert PatientJoinRequest.objects.get(id=req_a).status == PatientJoinRequest.STATUS_APPROVED
    other = PatientJoinRequest.objects.get(id=req_b)
    assert other.status == PatientJoinRequest.STATUS_WITHDRAWN, "her other request must close"
    assert other.decided_by is None, "no hospital made that decision, so none is credited"
    assert Patient.objects.count() == 1


def test_a_second_hospital_onboarding_her_is_409_not_a_crash(client, make_hospital, ready_patient, auth):
    """Patient.user is a one-to-one, so a second onboarding used to be an
    IntegrityError and surface as a 500."""
    alpha = make_hospital("Alpha First Hospital")
    beta = make_hospital("Beta Second Hospital")
    _, headers = ready_patient()
    req_a = send(client, alpha, headers).json()["id"]
    req_b = send(client, beta, headers).json()["id"]
    onboard(client, auth(alpha.admin.email), req_a)
    # Force it back to pending so the already-decided guard is not what answers.
    PatientJoinRequest.objects.filter(id=req_b).update(status=PatientJoinRequest.STATUS_PENDING)

    response = onboard(client, auth(beta.admin.email), req_b)

    assert response.status_code == 409
    assert "another hospital" in response.json()["detail"]
    assert Patient.objects.count() == 1, "no second record, and no crash"


def test_she_sees_the_withdrawal_on_her_own_list(client, make_hospital, ready_patient, auth):
    alpha = make_hospital("Alpha Visible Hospital")
    beta = make_hospital("Beta Visible Hospital")
    _, headers = ready_patient()
    req_a = send(client, alpha, headers).json()["id"]
    send(client, beta, headers)
    onboard(client, auth(alpha.admin.email), req_a)

    rows = client.get(MY_REQUESTS, **headers).json()["results"]

    assert sorted(r["status"] for r in rows) == ["approved", "withdrawn"]


# -- Withdrawing ------------------------------------------------------------------------


def withdraw_url(request_id):
    return f"{MY_REQUESTS}{request_id}/withdraw/"


def test_she_can_withdraw_a_pending_request(client, make_hospital, ready_patient):
    hospital = make_hospital("Withdraw Hospital")
    _, headers = ready_patient("withdraw@example.test")
    request_id = send(client, hospital, headers).json()["id"]

    response = post_json(client, withdraw_url(request_id), {}, headers)

    assert response.status_code == 200, response.content
    assert response.json()["status"] == PatientJoinRequest.STATUS_WITHDRAWN


def test_withdrawing_frees_her_to_apply_to_the_same_hospital_again(client, make_hospital, ready_patient):
    """The one-pending-request-per-hospital constraint counts only pending
    ones, so withdrawing must genuinely release the slot."""
    hospital = make_hospital("Reapply Hospital")
    _, headers = ready_patient("reapply@example.test")
    first_id = send(client, hospital, headers).json()["id"]
    post_json(client, withdraw_url(first_id), {}, headers)

    assert send(client, hospital, headers).status_code == 201


def test_she_cannot_withdraw_someone_elses_request(client, make_hospital, ready_patient):
    """Scoped to her own rows before the lookup, so another woman's request is
    404 -- a 403 would confirm it exists."""
    hospital = make_hospital("Not Hers Hospital")
    _, hers = ready_patient("mine@example.test")
    _, theirs = ready_patient("other@example.test")
    other_id = send(client, hospital, theirs).json()["id"]

    response = post_json(client, withdraw_url(other_id), {}, hers)

    assert response.status_code == 404
    assert PatientJoinRequest.objects.get(id=other_id).status == PatientJoinRequest.STATUS_PENDING


def test_she_cannot_withdraw_after_a_hospital_onboarded_her(client, make_hospital, ready_patient, auth):
    """Onboarding created a real Patient and a care relationship. Undoing that is
    a discharge, not a POST from the phone."""
    hospital = make_hospital("Already Approved Hospital")
    _, headers = ready_patient("approved@example.test")
    request_id = send(client, hospital, headers).json()["id"]
    assert onboard(client, auth(hospital.admin.email), request_id).status_code == 201

    response = post_json(client, withdraw_url(request_id), {}, headers)

    assert response.status_code == 409, response.content
    assert PatientJoinRequest.objects.get(id=request_id).status == PatientJoinRequest.STATUS_APPROVED


def test_she_cannot_withdraw_a_rejected_request(client, make_hospital, ready_patient, auth):
    """Withdrawing a rejection would rewrite the hospital's record of a decision
    it actually made."""
    hospital = make_hospital("Was Rejected Hospital")
    _, headers = ready_patient("rejected@example.test")
    request_id = send(client, hospital, headers).json()["id"]
    post_json(client, f"{REVIEW}{request_id}/reject/", {}, auth(hospital.admin.email))

    response = post_json(client, withdraw_url(request_id), {}, headers)

    assert response.status_code == 409, response.content
    assert PatientJoinRequest.objects.get(id=request_id).status == PatientJoinRequest.STATUS_REJECTED


def test_withdrawing_twice_is_refused_not_silently_repeated(client, make_hospital, ready_patient):
    hospital = make_hospital("Twice Hospital")
    _, headers = ready_patient("twice@example.test")
    request_id = send(client, hospital, headers).json()["id"]
    post_json(client, withdraw_url(request_id), {}, headers)

    assert post_json(client, withdraw_url(request_id), {}, headers).status_code == 409


def test_hospital_staff_cannot_withdraw_on_her_behalf(client, make_hospital, ready_patient, auth):
    """Withdraw is hers alone -- a hospital declining someone is a rejection,
    which is recorded as such with who decided it."""
    hospital = make_hospital("Staff Cannot Hospital")
    _, headers = ready_patient("staffcannot@example.test")
    request_id = send(client, hospital, headers).json()["id"]

    response = post_json(client, withdraw_url(request_id), {}, auth(hospital.admin.email))

    assert response.status_code == 403


def test_there_is_no_way_to_edit_a_submitted_request(client, make_hospital, ready_patient):
    """The snapshot is what staff read when deciding, so it must not change
    under them. Withdraw and resend instead."""
    hospital = make_hospital("No Edit Hospital")
    _, headers = ready_patient("noedit@example.test")
    request_id = send(client, hospital, headers).json()["id"]

    detail = f"{MY_REQUESTS}{request_id}/"
    for method in (client.patch, client.put, client.delete):
        response = method(
            detail, data=json.dumps({"profile": {"first_name": "Changed"}}), content_type="application/json", **headers
        )
        assert response.status_code in (404, 405), f"{method.__name__} returned {response.status_code}"
