"""A patient's identity document is a "national ID" (not every country issues a
CNIC), and a patient has no gender field at all: the platform is for women."""

import json

import pytest

from momcare_platform.core.patients.models import Patient

pytestmark = pytest.mark.django_db

PATIENTS = "/api/patients/"

BODY = {
    "first_name": "Ayesha",
    "last_name": "Bibi",
    "national_id": "61101-1234567-8",
    "address_line1": "House 12, Street 4",
    "address_line2": "F-7",
    "city": "Islamabad",
    "state": "ICT",
    "postal_code": "44000",
    "country": "Pakistan",
}


def enrol(client, headers, **overrides):
    return client.post(PATIENTS, data=json.dumps({**BODY, **overrides}), content_type="application/json", **headers)


def test_the_patient_detail_calls_it_national_id(client, make_hospital, auth):
    hospital = make_hospital("National Id Detail Hospital")

    body = enrol(client, auth(hospital.admin.email)).json()

    assert body["national_id"] == "61101-1234567-8"
    assert "cnic" not in body


def test_the_patient_list_calls_it_national_id(client, make_hospital, auth):
    hospital = make_hospital("National Id List Hospital")
    headers = auth(hospital.admin.email)
    enrol(client, headers)

    row = client.get(PATIENTS, **headers).json()["results"][0]

    assert row["national_id"] == "61101-1234567-8"
    assert "cnic" not in row


def test_search_finds_a_patient_by_national_id(client, make_hospital, auth):
    hospital = make_hospital("National Id Search Hospital")
    headers = auth(hospital.admin.email)
    enrol(client, headers)

    body = client.get(f"{PATIENTS}?search=1234567-8", **headers).json()

    assert body["count"] == 1


def test_a_patient_has_no_gender(client, make_hospital, auth):
    hospital = make_hospital("No Gender Hospital")
    headers = auth(hospital.admin.email)

    created = enrol(client, headers, gender="female").json()
    listed = client.get(PATIENTS, **headers).json()["results"][0]

    assert "gender" not in created
    assert "gender" not in listed
    assert not hasattr(Patient.objects.get(id=created["id"]), "gender")
