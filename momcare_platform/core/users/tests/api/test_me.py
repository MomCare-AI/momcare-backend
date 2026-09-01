"""/api/auth/me/ - the signed-in user's own profile.

``staff_id`` exists so the frontend can match "me" against a care-team
row's staff id without a second round-trip (e.g. to decide whether to show
a pregnancy's care-team write controls to the logged-in user).
"""

import pytest
from django.conf import settings

pytestmark = pytest.mark.django_db

ME = "/api/auth/me/"


def test_a_staff_linked_user_gets_their_own_staff_id(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Me Staff Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@mestaff.test")

    response = client.get(ME, **auth(nurse.email))

    assert response.status_code == 200
    assert response.json()["staff_id"] == str(nurse.staff.id)


def test_a_user_with_no_staff_row_gets_a_null_staff_id(client, make_hospital, auth):
    """hospital_admin, in this fixture, has no Staff row - the field must
    say so honestly rather than erroring or omitting itself."""
    hospital = make_hospital("Me No Staff Hospital")

    response = client.get(ME, **auth(hospital.admin.email))

    assert response.status_code == 200
    assert response.json()["staff_id"] is None
