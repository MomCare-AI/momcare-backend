"""A patient's postal address -- stored on the Patient row itself (the six
AddressMixin columns), so a walk-in with no app account still has one.
Every field required at hospital onboarding and at self-registration, where
it is stored on her User and copied onto the Patient on approval."""

import json

import pytest

from momcare_platform.core.patients.models import Patient
from momcare_platform.core.patients.tests.conftest import PROFILE_BODY
from momcare_platform.core.users.models import User

pytestmark = pytest.mark.django_db

PATIENTS = "/api/patients/"
REGISTER = "/api/auth/patient/register/"
MY_REQUESTS = "/api/my-requests/"
REVIEW = "/api/patient-requests/"

ADDRESS = {
    "address_line1": "House 12, Street 4",
    "address_line2": "F-7",
    "city": "Islamabad",
    "state": "ICT",
    "postal_code": "44000",
    "country": "Pakistan",
}


def post(client, url, body, headers):
    return client.post(url, data=json.dumps(body), content_type="application/json", **headers)


def patch(client, url, body, headers):
    return client.patch(url, data=json.dumps(body), content_type="application/json", **headers)


def test_address_is_stored_at_onboarding_and_returned_on_detail(client, make_hospital, auth):
    hospital = make_hospital("Address Onboarding Hospital")
    headers = auth(hospital.admin.email)

    created = post(client, PATIENTS, {"first_name": "Ayesha", **ADDRESS}, headers)

    assert created.status_code == 201, created.content
    for field, value in ADDRESS.items():
        assert created.json()[field] == value
    detail = client.get(f"{PATIENTS}{created.json()['id']}/", **headers).json()
    assert detail["city"] == "Islamabad"
    assert Patient.objects.get().user is None, "a walk-in has no User -- the address must not depend on one"


def test_every_address_field_is_required_at_onboarding(client, make_hospital, auth):
    hospital = make_hospital("Address Required Hospital")
    headers = auth(hospital.admin.email)

    missing_all = post(client, PATIENTS, {"first_name": "Ayesha"}, headers)
    missing_city = post(
        client,
        PATIENTS,
        {"first_name": "Ayesha", **{k: v for k, v in ADDRESS.items() if k != "city"}},
        headers,
    )

    assert missing_all.status_code == 400
    assert set(ADDRESS) <= set(missing_all.json())
    assert missing_city.status_code == 400
    assert "city" in missing_city.json()


def test_address_can_be_updated(client, make_hospital, auth):
    hospital = make_hospital("Address Update Hospital")
    headers = auth(hospital.admin.email)
    patient_id = post(client, PATIENTS, {"first_name": "Ayesha", **ADDRESS}, headers).json()["id"]

    response = patch(client, f"{PATIENTS}{patient_id}/", {"city": "Lahore", "postal_code": "54000"}, headers)

    assert response.status_code == 200, response.content
    assert response.json()["city"] == "Lahore"
    assert response.json()["address_line1"] == ADDRESS["address_line1"], "untouched fields stay"


def test_registration_requires_every_address_field(client):
    response = client.post(
        REGISTER,
        data=json.dumps({"email": "noaddr@example.test", "password": "HerOwnPick!2026", "first_name": "Ayesha"}),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert set(ADDRESS) <= set(response.json())


def _register_and_send(client, hospital, email, address, phone):
    """Sign up with an address, fill in the profile, send a request; returns (user, request id)."""
    registered = client.post(
        REGISTER,
        data=json.dumps(
            {
                "email": email,
                "password": "HerOwnPick!2026",
                "first_name": email[:3],
                "last_name": "Bibi",
                **address,
            },
        ),
        content_type="application/json",
    )
    assert registered.status_code == 201, registered.content
    User.objects.filter(email=email).update(is_email_verified=True)
    login = client.post(
        "/api/auth/login/",
        data=json.dumps({"email": email, "password": "HerOwnPick!2026"}),
        content_type="application/json",
    ).json()
    her = {"HTTP_AUTHORIZATION": f"Bearer {login['access']}"}
    filled = patch(client, "/api/my-profile/", {"phone": phone, **PROFILE_BODY}, her)
    assert filled.status_code == 200, filled.content
    sent = post(client, MY_REQUESTS, {"organization": str(hospital.org.id)}, her)
    assert sent.status_code == 201, sent.content
    return User.objects.get(email=email), sent.json()["id"]


def _save_onboarding_form(client, staff, request_id):
    """What the web app does: pre-fill the form from the request, save it with the request's id."""
    detail = client.get(f"{REVIEW}{request_id}/", **staff).json()
    return post(client, PATIENTS, {**detail["profile"], "join_request": request_id}, staff)


def test_address_given_at_registration_is_copied_to_the_patient_on_approval(client, make_hospital, auth):
    hospital = make_hospital("Address Join Hospital")
    user, request_id = _register_and_send(client, hospital, "applicant@example.test", ADDRESS, "03001110001")
    assert user.city == "Islamabad", "stored on her account at registration"

    response = _save_onboarding_form(client, auth(hospital.admin.email), request_id)

    assert response.status_code == 201, response.content
    patient = Patient.objects.get()
    assert patient.address_line1 == ADDRESS["address_line1"]
    assert patient.city == "Islamabad"
    assert patient.user == user


def test_each_approved_patient_gets_her_own_address_not_anyone_elses(client, make_hospital, auth):
    hospital = make_hospital("Two Addresses Hospital")
    other = {**ADDRESS, "address_line1": "Flat 9, Sunrise Apartments", "city": "Lahore"}
    for email, address, phone in [
        ("one@example.test", ADDRESS, "03001110002"),
        ("two@example.test", other, "03001110003"),
    ]:
        _, request_id = _register_and_send(client, hospital, email, address, phone)
        # Distinct national IDs: one hospital never holds two patients with the same one.
        detail = client.get(f"{REVIEW}{request_id}/", **auth(hospital.admin.email)).json()
        form = {**detail["profile"], "national_id": phone, "join_request": request_id}
        assert post(client, PATIENTS, form, auth(hospital.admin.email)).status_code == 201

    assert Patient.objects.get(user__email="one@example.test").city == "Islamabad"
    assert Patient.objects.get(user__email="two@example.test").city == "Lahore"
    assert Patient.objects.get(user__email="two@example.test").address_line1 == "Flat 9, Sunrise Apartments"
