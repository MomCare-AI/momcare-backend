"""The profile a woman fills in once, after signing up and before she asks any
hospital to take her on. Identity only: nothing medical.

Name and address come from sign-up (they live on ``User``); the profile step
adds what ``User`` lacks. ``is_complete`` is what the app uses to decide
whether to show the profile form, and what the join request checks.
"""

import pytest

from momcare_platform.core.patients.models import PatientProfile
from momcare_platform.core.patients.tests.conftest import MY_PROFILE, PROFILE_BODY, patch_json, post_json

pytestmark = pytest.mark.django_db

NOT_ASKED_AT_SIGNUP = {
    "date_of_birth",
    "phone",
    "emergency_contact_name",
    "emergency_contact_phone",
    "emergency_contact_relation",
    "emergency_contact_email",
}


# -- Reading it ----------------------------------------------------------------


def test_a_new_account_has_an_incomplete_profile(client, registered_patient):
    _, headers = registered_patient()

    body = client.get(MY_PROFILE, **headers).json()

    assert body["is_complete"] is False
    assert set(body["missing_fields"]) == NOT_ASKED_AT_SIGNUP


def test_the_profile_shows_what_sign_up_already_collected(client, registered_patient):
    _, headers = registered_patient()

    body = client.get(MY_PROFILE, **headers).json()

    assert body["email"] == "ayesha@example.test"
    assert body["first_name"] == "Ayesha"
    assert body["last_name"] == "Bibi"
    assert body["city"] == "Islamabad"
    assert body["national_id"] is None
    assert "gender" not in body


def test_the_profile_needs_a_signed_in_user(client):
    assert client.get(MY_PROFILE).status_code == 401


def test_hospital_staff_cannot_use_the_patient_profile(client, make_hospital, auth):
    hospital = make_hospital("Staff Profile Hospital")

    response = client.get(MY_PROFILE, **auth(hospital.admin.email))

    assert response.status_code == 403


# -- Filling it in ---------------------------------------------------------------


def test_filling_in_the_rest_makes_it_complete(client, registered_patient):
    user, headers = registered_patient()

    response = patch_json(client, MY_PROFILE, {"phone": "03001230001", **PROFILE_BODY}, headers)

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["is_complete"] is True
    assert body["missing_fields"] == []
    user.refresh_from_db()
    assert str(user.date_of_birth) == "1996-04-12"
    assert user.phone == "03001230001"
    profile = PatientProfile.objects.get(user=user)
    assert profile.national_id == "61101-7654321-0"
    assert profile.blood_group == "O+"
    assert profile.emergency_contact_name == "Bilal Ahmed"


def test_it_can_be_filled_in_across_several_saves(client, registered_patient):
    _, headers = registered_patient()

    patch_json(client, MY_PROFILE, {"phone": "03001230002", "date_of_birth": "1996-04-12"}, headers)
    body = patch_json(client, MY_PROFILE, {"emergency_contact_name": "Bilal Ahmed"}, headers).json()

    assert body["is_complete"] is False
    assert set(body["missing_fields"]) == {
        "emergency_contact_phone",
        "emergency_contact_relation",
        "emergency_contact_email",
    }


def test_national_id_and_blood_group_are_optional(client, registered_patient):
    _, headers = registered_patient()
    body = {k: v for k, v in PROFILE_BODY.items() if k not in ("national_id", "blood_group")}

    response = patch_json(client, MY_PROFILE, {"phone": "03001230003", **body}, headers)

    assert response.json()["is_complete"] is True


def test_every_emergency_contact_field_is_required(client, registered_patient):
    _, headers = registered_patient()
    body = {k: v for k, v in PROFILE_BODY.items() if k != "emergency_contact_email"}

    response = patch_json(client, MY_PROFILE, {"phone": "03001230004", **body}, headers)

    assert response.json()["is_complete"] is False
    assert response.json()["missing_fields"] == ["emergency_contact_email"]


def test_the_email_cannot_be_changed_here(client, registered_patient):
    user, headers = registered_patient()

    patch_json(client, MY_PROFILE, {"email": "someoneelse@example.test"}, headers)

    user.refresh_from_db()
    assert user.email == "ayesha@example.test"


def test_a_required_field_cannot_be_blanked(client, registered_patient):
    _, headers = registered_patient()

    for field in ("first_name", "last_name", "address_line1", "city"):
        response = patch_json(client, MY_PROFILE, {field: ""}, headers)
        assert response.status_code == 400, field
        assert field in response.json()


def test_a_phone_number_already_in_use_is_refused(client, registered_patient):
    _, first = registered_patient(email="first@example.test")
    _, second = registered_patient(email="second@example.test")
    patch_json(client, MY_PROFILE, {"phone": "03005550000"}, first)

    response = patch_json(client, MY_PROFILE, {"phone": "03005550000"}, second)

    assert response.status_code == 400
    assert "phone" in response.json()


def test_resaving_her_own_phone_is_not_a_conflict(client, registered_patient):
    _, headers = registered_patient()
    patch_json(client, MY_PROFILE, {"phone": "03005551111"}, headers)

    response = patch_json(client, MY_PROFILE, {"phone": "03005551111"}, headers)

    assert response.status_code == 200


def test_a_birth_date_in_the_future_is_refused(client, registered_patient):
    _, headers = registered_patient()

    response = patch_json(client, MY_PROFILE, {"date_of_birth": "2999-01-01"}, headers)

    assert response.status_code == 400
    assert "date_of_birth" in response.json()


def test_an_unknown_blood_group_is_refused(client, registered_patient):
    _, headers = registered_patient()

    response = patch_json(client, MY_PROFILE, {"blood_group": "Z+"}, headers)

    assert response.status_code == 400


def test_a_blank_national_id_is_stored_as_none(client, registered_patient):
    """Two women with no national ID must never collide on the same blank."""
    user, headers = registered_patient()

    patch_json(client, MY_PROFILE, {"national_id": ""}, headers)

    assert PatientProfile.objects.get(user=user).national_id is None


# -- Whose it is -----------------------------------------------------------------


def test_each_woman_sees_and_changes_only_her_own_profile(client, registered_patient):
    _, mine = registered_patient(email="mine@example.test")
    _, hers = registered_patient(email="hers@example.test")
    patch_json(client, MY_PROFILE, {"emergency_contact_name": "Mine Only"}, mine)

    body = client.get(MY_PROFILE, **hers).json()

    assert body["email"] == "hers@example.test"
    assert body["emergency_contact_name"] == ""


# -- Sign-up and /me -------------------------------------------------------------


def test_signing_up_now_requires_a_last_name(client):
    response = post_json(
        client,
        "/api/auth/patient/register/",
        {
            "email": "nolast@example.test",
            "password": "HerOwnPick!2026",
            "first_name": "Ayesha",
            "address_line1": "House 12, Street 4",
            "address_line2": "F-7",
            "city": "Islamabad",
            "state": "ICT",
            "postal_code": "44000",
            "country": "Pakistan",
        },
    )

    assert response.status_code == 400
    assert "last_name" in response.json()


def test_me_tells_a_patient_whether_her_profile_is_complete(client, registered_patient, complete_profile):
    _, headers = registered_patient()

    assert client.get("/api/auth/me/", **headers).json()["profile_complete"] is False
    complete_profile(headers)
    assert client.get("/api/auth/me/", **headers).json()["profile_complete"] is True


def test_me_for_staff_has_no_profile_complete_key(client, make_hospital, auth):
    hospital = make_hospital("Staff Me Hospital")

    body = client.get("/api/auth/me/", **auth(hospital.admin.email)).json()

    assert "profile_complete" not in body
