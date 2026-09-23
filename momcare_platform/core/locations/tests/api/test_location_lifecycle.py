"""Deactivate / reactivate / move-patients / assignment-status / patients —
permission matrix.

Deactivate and move-patients follow the same hospital_admin-OR-own-manager
split as Update (see test_locations.py); reactivate stays hospital_admin-only,
same as create — see locations/api/views.py's module docstring for why.
"""

import json

import pytest
from django.conf import settings

from momcare_platform.core.locations.models import Location
from momcare_platform.core.locations.services import ensure_default_location
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db


def detail_url(location_id):
    return f"/api/locations/{location_id}/"


def deactivate_url(location_id):
    return f"/api/locations/{location_id}/deactivate/"


def reactivate_url(location_id):
    return f"/api/locations/{location_id}/reactivate/"


def move_patients_url(location_id):
    return f"/api/locations/{location_id}/move-patients/"


def assignment_status_url(location_id):
    return f"/api/locations/{location_id}/assignment-status/"


def patients_url(location_id):
    return f"/api/locations/{location_id}/patients/"


def post(client, headers, url, **fields):
    return client.post(url, data=json.dumps(fields), content_type="application/json", **headers)


def make_manager_and_location(make_hospital, make_staff, hospital_name, role_code, manager_email):
    hospital = make_hospital(hospital_name)
    manager = make_staff(hospital.org, role_code, manager_email)
    location = ensure_default_location(hospital.org)
    location.location_manager = manager
    location.save(update_fields=["location_manager", "updated_at"])
    return hospital, manager, location


# --- Deactivate ---------------------------------------------------------


def test_hospital_admin_can_deactivate_an_empty_location(client, make_hospital, auth):
    hospital = make_hospital("Admin Deactivate Hospital")
    location = ensure_default_location(hospital.org)

    response = post(client, auth(hospital.admin.email), deactivate_url(location.id), reason="Closing.")

    assert response.status_code == 200, response.content
    location.refresh_from_db()
    assert location.is_active is False


def test_the_locations_own_manager_can_deactivate_it(client, make_hospital, make_staff, auth):
    _, manager, location = make_manager_and_location(
        make_hospital, make_staff, "Manager Deactivate Hospital", settings.ROLE_NURSE, "nurse@managerdeactivate.test"
    )

    response = post(client, auth(manager.email), deactivate_url(location.id), reason="Closing.")

    assert response.status_code == 200, response.content
    location.refresh_from_db()
    assert location.is_active is False


def test_a_staff_member_who_is_not_the_manager_cannot_deactivate(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Non Manager Deactivate Hospital")
    location = ensure_default_location(hospital.org)
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@nonmanagerdeactivate.test")

    response = post(client, auth(nurse.email), deactivate_url(location.id))

    assert response.status_code == 403
    location.refresh_from_db()
    assert location.is_active is True


def test_deactivate_is_still_blocked_by_active_patients_for_the_manager_too(
    client,
    make_hospital,
    make_staff,
    auth,
):
    _, manager, location = make_manager_and_location(
        make_hospital, make_staff, "Occupied Manager Hospital", settings.ROLE_NURSE, "nurse@occupiedmanager.test"
    )
    onboard_patient(
        organization=location.organization,
        patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
    )

    response = post(client, auth(manager.email), deactivate_url(location.id))

    assert response.status_code == 400
    location.refresh_from_db()
    assert location.is_active is True


# --- Reactivate — hospital_admin only, even for the location's own manager --


def test_hospital_admin_can_reactivate(client, make_hospital, auth):
    hospital = make_hospital("Admin Reactivate Hospital")
    location = ensure_default_location(hospital.org)
    location.deactivate()

    response = post(client, auth(hospital.admin.email), reactivate_url(location.id))

    assert response.status_code == 200, response.content
    location.refresh_from_db()
    assert location.is_active is True


def test_the_locations_own_manager_cannot_reactivate_it(client, make_hospital, make_staff, auth):
    _, manager, location = make_manager_and_location(
        make_hospital, make_staff, "Manager Reactivate Hospital", settings.ROLE_NURSE, "nurse@managerreactivate.test"
    )
    location.deactivate()

    response = post(client, auth(manager.email), reactivate_url(location.id))

    assert response.status_code == 403
    location.refresh_from_db()
    assert location.is_active is False


# --- Move patients --------------------------------------------------------


def test_hospital_admin_can_move_patients(client, make_hospital, auth):
    hospital = make_hospital("Admin Move Hospital")
    source = ensure_default_location(hospital.org)
    target = Location.objects.create(organization=hospital.org, name="Target Branch", location_manager=hospital.admin)
    onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
    )

    response = post(
        client,
        auth(hospital.admin.email),
        move_patients_url(source.id),
        target_location_id=str(target.id),
        move_all=True,
    )

    assert response.status_code == 200, response.content
    assert target.patients.count() == 1


def test_the_source_locations_own_manager_can_move_its_patients(client, make_hospital, make_staff, auth):
    _, manager, source = make_manager_and_location(
        make_hospital, make_staff, "Manager Move Hospital", settings.ROLE_NURSE, "nurse@managermove.test"
    )
    target = Location.objects.create(organization=source.organization, name="Target Branch", location_manager=manager)
    onboard_patient(
        organization=source.organization,
        patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
    )

    response = post(
        client,
        auth(manager.email),
        move_patients_url(source.id),
        target_location_id=str(target.id),
        move_all=True,
    )

    assert response.status_code == 200, response.content
    assert target.patients.count() == 1


def test_a_staff_member_who_is_not_the_source_manager_cannot_move_patients(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Non Manager Move Hospital")
    source = ensure_default_location(hospital.org)
    target = Location.objects.create(organization=hospital.org, name="Target Branch", location_manager=hospital.admin)
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@nonmanagermove.test")

    response = post(
        client,
        auth(nurse.email),
        move_patients_url(source.id),
        target_location_id=str(target.id),
        move_all=True,
    )

    assert response.status_code == 403


# --- Read-only sub-resources: any hospital staff, manager or not ---------


def test_any_hospital_staff_can_read_assignment_status(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Assignment Status Hospital")
    location = ensure_default_location(hospital.org)
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@assignmentstatus.test")

    response = client.get(assignment_status_url(location.id), **auth(nurse.email))

    assert response.status_code == 200
    assert response.json()["has_active_patients"] is False


def test_any_hospital_staff_can_read_the_patients_sub_resource(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Patients Subresource Hospital")
    location = ensure_default_location(hospital.org)
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@patientssubresource.test")

    response = client.get(patients_url(location.id), **auth(nurse.email))

    assert response.status_code == 200


# --- Cross-tenant: 404, never 403 -----------------------------------------


def test_deactivating_another_hospitals_location_404s(client, make_hospital, auth):
    ours = make_hospital("Deactivate Ours Hospital")
    theirs = make_hospital("Deactivate Theirs Hospital")
    their_location = ensure_default_location(theirs.org)

    response = post(client, auth(ours.admin.email), deactivate_url(their_location.id))

    assert response.status_code == 404


def test_moving_patients_out_of_another_hospitals_location_404s(client, make_hospital, auth):
    ours = make_hospital("Move Ours Hospital")
    theirs = make_hospital("Move Theirs Hospital")
    their_location = ensure_default_location(theirs.org)

    response = post(
        client,
        auth(ours.admin.email),
        move_patients_url(their_location.id),
        target_location_id=str(their_location.id),
        move_all=True,
    )

    assert response.status_code == 404


# --- Delete ----------------------------------------------------------------


def test_deleting_an_active_location_is_refused(client, make_hospital, auth):
    hospital = make_hospital("Delete Active Hospital")
    location = ensure_default_location(hospital.org)

    response = client.delete(detail_url(location.id), **auth(hospital.admin.email))

    assert response.status_code == 400
    assert Location.objects.filter(pk=location.id).exists()


def test_hospital_admin_can_delete_a_deactivated_location_with_no_patient_history(client, make_hospital, auth):
    hospital = make_hospital("Delete Clean Hospital")
    location = ensure_default_location(hospital.org)
    location.deactivate()

    response = client.delete(detail_url(location.id), **auth(hospital.admin.email))

    assert response.status_code == 204
    assert not Location.objects.filter(pk=location.id).exists()


def test_the_locations_own_manager_cannot_delete_it(client, make_hospital, make_staff, auth):
    _, manager, location = make_manager_and_location(
        make_hospital, make_staff, "Manager Delete Hospital", settings.ROLE_NURSE, "nurse@managerdelete.test"
    )
    location.deactivate()

    response = client.delete(detail_url(location.id), **auth(manager.email))

    assert response.status_code == 403
    assert Location.objects.filter(pk=location.id).exists()


def test_deleting_a_location_with_patient_history_is_refused_not_500(client, make_hospital, auth):
    """Even a *deactivated* patient still references this location
    (``Patient.location`` is ``on_delete=PROTECT``, and patients are never
    hard-deleted) — so the location's own deactivation (zero *active*
    patients) is not enough to make it deletable."""
    hospital = make_hospital("Delete History Hospital")
    location = ensure_default_location(hospital.org)
    patient = onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
    )
    patient.deactivate()
    location.refresh_from_db()
    location.deactivate()  # legitimate now: zero active patients

    response = client.delete(detail_url(location.id), **auth(hospital.admin.email))

    assert response.status_code == 400
    assert Location.objects.filter(pk=location.id).exists()


def test_deleting_another_hospitals_location_404s(client, make_hospital, auth):
    ours = make_hospital("Delete Ours Hospital")
    theirs = make_hospital("Delete Theirs Hospital")
    their_location = ensure_default_location(theirs.org)
    their_location.deactivate()

    response = client.delete(detail_url(their_location.id), **auth(ours.admin.email))

    assert response.status_code == 404
