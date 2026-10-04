"""A staff member's postal address -- the six AddressMixin columns on their
User, accepted at onboarding and update and returned wherever the staff
member already is."""

import json

import pytest
from django.conf import settings

from momcare_platform.core.locations.models import Location

pytestmark = pytest.mark.django_db

STAFF_URL = "/api/staff/"

ADDRESS = {
    "address_line1": "Flat 3, Block B",
    "address_line2": "Near Park",
    "city": "Karachi",
    "state": "Sindh",
    "postal_code": "75500",
    "country": "Pakistan",
}


def post(client, headers, **fields):
    return client.post(STAFF_URL, data=json.dumps(fields), content_type="application/json", **headers)


def patch(client, headers, staff_id, **fields):
    return client.patch(
        f"{STAFF_URL}{staff_id}/",
        data=json.dumps(fields),
        content_type="application/json",
        **headers,
    )


def invite(client, headers, hospital, email, **extra):
    location = Location.objects.filter(organization=hospital.org).first() or Location.objects.create(
        organization=hospital.org,
        name="Main Branch",
    )
    return post(
        client,
        headers,
        email=email,
        role_code=settings.ROLE_NURSE,
        locations=[str(location.id)],
        **extra,
    )


def test_address_is_stored_at_onboarding_and_returned(client, make_hospital, auth):
    hospital = make_hospital("Staff Address Hospital")
    headers = auth(hospital.admin.email)

    response = invite(client, headers, hospital, "nurse@staffaddress.test", first_name="Sana", **ADDRESS)

    assert response.status_code == 201, response.content
    for field, value in ADDRESS.items():
        assert response.json()[field] == value
    listed = client.get(STAFF_URL, **headers).json()["results"]
    assert any(row["city"] == "Karachi" for row in listed)


def test_every_address_field_is_required_at_onboarding(client, make_hospital, auth):
    hospital = make_hospital("Staff Address Required Hospital")
    headers = auth(hospital.admin.email)
    location = Location.objects.create(organization=hospital.org, name="Main Branch")

    missing_all = post(
        client,
        headers,
        email="a@required.test",
        role_code=settings.ROLE_NURSE,
        locations=[str(location.id)],
    )
    missing_city = post(
        client,
        headers,
        email="b@required.test",
        role_code=settings.ROLE_NURSE,
        locations=[str(location.id)],
        **{k: v for k, v in ADDRESS.items() if k != "city"},
    )

    assert missing_all.status_code == 400
    assert set(ADDRESS) <= set(missing_all.json())
    assert missing_city.status_code == 400
    assert "city" in missing_city.json()


def test_address_can_be_updated(client, make_hospital, auth):
    hospital = make_hospital("Staff Address Update Hospital")
    headers = auth(hospital.admin.email)
    staff_id = invite(client, headers, hospital, "nurse@addressupdate.test", **ADDRESS).json()["id"]

    response = patch(client, headers, staff_id, city="Lahore", postal_code="54000")

    assert response.status_code == 200, response.content
    assert response.json()["city"] == "Lahore"
    assert response.json()["address_line1"] == ADDRESS["address_line1"], "untouched fields stay"
