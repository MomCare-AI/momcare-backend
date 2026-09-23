"""List/create/detail/update — a hospital managing its own sites.

No platform-admin approval anywhere here: create/update take effect
immediately, exactly like every other hospital-internal action in this
system that isn't the review gate itself.

Update is hospital_admin OR this location's own manager (mirrors
Neuro_RPM's Admin | LocationAdmin split — see locations/api/views.py's
module docstring); create stays hospital_admin-only. Deactivate/reactivate/
move-patients get the same treatment in test_location_lifecycle.py.
"""

import json

import pytest
from django.conf import settings

from momcare_platform.core.locations.models import Location
from momcare_platform.core.locations.services import ensure_default_location

pytestmark = pytest.mark.django_db

LIST_URL = "/api/locations/"


def detail_url(location_id):
    return f"/api/locations/{location_id}/"


def post(client, headers, **fields):
    return client.post(LIST_URL, data=json.dumps(fields), content_type="application/json", **headers)


def patch(client, headers, url, **fields):
    return client.patch(url, data=json.dumps(fields), content_type="application/json", **headers)


def test_a_new_hospital_already_has_its_main_branch_listed(client, make_hospital, auth):
    hospital = make_hospital("Listed Hospital")
    ensure_default_location(hospital.org)

    response = client.get(LIST_URL, **auth(hospital.admin.email))

    assert response.status_code == 200, response.content
    names = {row["name"] for row in response.json()["results"]}
    assert "Main Branch" in names


def test_a_hospital_admin_can_add_a_second_location(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Expanding Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@expanding.test")

    response = post(
        client,
        auth(hospital.admin.email),
        name="North Wing",
        address_line1="9 North Road",
        city="Lahore",
        state="Punjab",
        postal_code="54000",
        country="Pakistan",
        timezone="Asia/Karachi",
        location_manager=str(nurse.id),
    )

    assert response.status_code == 201, response.content
    body = response.json()
    assert body["name"] == "North Wing"
    assert body["location_manager_name"] == nurse.get_full_name()


def test_creating_a_location_without_a_manager_is_refused(client, make_hospital, auth):
    hospital = make_hospital("No Manager Hospital")

    response = post(client, auth(hospital.admin.email), name="Bare Branch")

    assert response.status_code == 400
    assert "location_manager" in response.json()
    assert not Location.objects.filter(name="Bare Branch").exists()


def test_a_manager_from_another_hospital_is_rejected(client, make_hospital, make_staff, auth):
    ours = make_hospital("Ours Hospital")
    theirs = make_hospital("Theirs Hospital")
    their_nurse = make_staff(theirs.org, settings.ROLE_NURSE, "nurse@theirs.test")

    response = post(client, auth(ours.admin.email), name="Suspicious Branch", location_manager=str(their_nurse.id))

    assert response.status_code == 400
    assert "location_manager" in response.json()


def test_a_patient_cannot_be_named_as_location_manager(client, make_hospital, django_user_model, auth):
    from momcare_platform.core.users.models import Role

    hospital = make_hospital("Patient Manager Hospital")
    patient_user = django_user_model.objects.create_user(
        email="patient@patientmanager.test",
        password="Sup3rSecret!",
        first_name="A",
        last_name="Patient",
        role=Role.objects.get(code=settings.ROLE_PATIENT),
    )
    patient_user.organization = hospital.org
    patient_user.save(update_fields=["organization", "updated_at"])

    response = post(client, auth(hospital.admin.email), name="Odd Branch", location_manager=str(patient_user.id))

    assert response.status_code == 400
    assert "location_manager" in response.json()


def test_duplicate_location_names_within_one_hospital_are_refused(client, make_hospital, auth):
    hospital = make_hospital("Duplicate Name Hospital")
    ensure_default_location(hospital.org)  # already named "Main Branch"

    response = post(client, auth(hospital.admin.email), name="Main Branch", location_manager=str(hospital.admin.id))

    assert response.status_code == 400
    assert "name" in response.json()


def test_the_same_name_is_fine_at_a_different_hospital(client, make_hospital, auth):
    ours = make_hospital("First Hospital")
    theirs = make_hospital("Second Hospital")
    ensure_default_location(theirs.org)  # also has a "Main Branch"

    response = post(client, auth(ours.admin.email), name="Main Branch", location_manager=str(ours.admin.id))

    assert response.status_code == 201, response.content


def test_a_non_admin_cannot_create_a_location(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Locked Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@locked.test")

    response = post(client, auth(nurse.email), name="Should Fail", location_manager=str(hospital.admin.id))

    assert response.status_code == 403
    assert not Location.objects.filter(name="Should Fail").exists()


def test_any_hospital_staff_can_read_the_location_list(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Readable Hospital")
    ensure_default_location(hospital.org)
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@readable.test")

    response = client.get(LIST_URL, **auth(nurse.email))

    assert response.status_code == 200


def test_another_hospitals_locations_are_never_visible(client, make_hospital, auth):
    ours = make_hospital("Ours Only Hospital")
    theirs = make_hospital("Theirs Only Hospital")
    ensure_default_location(theirs.org)

    response = client.get(LIST_URL, **auth(ours.admin.email))

    names = {row["name"] for row in response.json()["results"]}
    assert names == set()


def test_a_hospital_admin_can_update_a_location(client, make_hospital, auth):
    hospital = make_hospital("Updatable Hospital")
    location = ensure_default_location(hospital.org)

    response = patch(client, auth(hospital.admin.email), detail_url(location.id), phone="0599998888", city="Lahore")

    assert response.status_code == 200, response.content
    location.refresh_from_db()
    assert location.phone == "0599998888"
    assert location.city == "Lahore"


def test_updating_can_omit_the_manager_without_clearing_it(client, make_hospital, auth):
    hospital = make_hospital("Manager Kept Hospital")
    location = ensure_default_location(hospital.org)
    original_manager_id = location.location_manager_id

    response = patch(client, auth(hospital.admin.email), detail_url(location.id), phone="0511112222")

    assert response.status_code == 200, response.content
    location.refresh_from_db()
    assert location.location_manager_id == original_manager_id


def test_a_hospital_admin_can_hand_the_manager_role_to_a_care_manager(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Reassigned Manager Hospital")
    location = ensure_default_location(hospital.org)
    care_manager = make_staff(hospital.org, settings.ROLE_CARE_MANAGER, "cm@reassigned.test")

    response = patch(
        client, auth(hospital.admin.email), detail_url(location.id), location_manager=str(care_manager.id)
    )

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["location_manager"] == str(care_manager.id)
    assert body["location_manager_name"] == care_manager.get_full_name()
    location.refresh_from_db()
    assert location.location_manager_id == care_manager.id


def test_a_locations_own_manager_can_update_it_without_being_hospital_admin(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Manager Self Service Hospital")
    manager = make_staff(hospital.org, settings.ROLE_NURSE, "nurse-manager@selfservice.test")
    location = ensure_default_location(hospital.org)
    location.location_manager = manager
    location.save(update_fields=["location_manager", "updated_at"])

    response = patch(client, auth(manager.email), detail_url(location.id), phone="0533334444", city="Karachi")

    assert response.status_code == 200, response.content
    location.refresh_from_db()
    assert location.phone == "0533334444"
    assert location.city == "Karachi"


def test_a_locations_manager_can_hand_off_to_a_successor(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Handoff Hospital")
    outgoing = make_staff(hospital.org, settings.ROLE_NURSE, "outgoing@handoff.test")
    incoming = make_staff(hospital.org, settings.ROLE_CARE_MANAGER, "incoming@handoff.test")
    location = ensure_default_location(hospital.org)
    location.location_manager = outgoing
    location.save(update_fields=["location_manager", "updated_at"])

    response = patch(client, auth(outgoing.email), detail_url(location.id), location_manager=str(incoming.id))

    assert response.status_code == 200, response.content
    location.refresh_from_db()
    assert location.location_manager_id == incoming.id


def test_a_different_locations_manager_cannot_update_this_one(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Cross Manager Hospital")
    location_a = ensure_default_location(hospital.org)
    manager_b = make_staff(hospital.org, settings.ROLE_NURSE, "manager-b@crossmanager.test")
    Location.objects.create(organization=hospital.org, name="Branch B", location_manager=manager_b)

    response = patch(client, auth(manager_b.email), detail_url(location_a.id), phone="0500000001")

    assert response.status_code == 403


def test_a_non_admin_cannot_update_a_location(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Update Locked Hospital")
    location = ensure_default_location(hospital.org)
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@updatelocked.test")

    response = patch(client, auth(nurse.email), detail_url(location.id), phone="0500000000")

    assert response.status_code == 403


def test_another_hospitals_location_detail_404s_not_403s(client, make_hospital, auth):
    ours = make_hospital("Detail Ours Hospital")
    theirs = make_hospital("Detail Theirs Hospital")
    their_location = ensure_default_location(theirs.org)

    response = client.get(detail_url(their_location.id), **auth(ours.admin.email))

    assert response.status_code == 404


def test_an_anonymous_caller_is_refused(client):
    assert client.get(LIST_URL).status_code in (401, 403)
