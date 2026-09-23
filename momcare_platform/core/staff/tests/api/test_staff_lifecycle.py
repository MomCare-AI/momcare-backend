"""Update / deactivate / reactivate / delete / assignment-status for an
existing staff member — the admin-level counterpart to onboarding.

Same Admin-or-own-manager split used throughout this app: hospital_admin
manages every staff member; a location manager manages only staff assigned
to (at least one of) the location(s) they themselves manage.
"""

import datetime
import json

import pytest
from django.conf import settings

from momcare_platform.core.locations.models import Location
from momcare_platform.core.patients.services import onboard_patient
from momcare_platform.core.staff.models import Staff
from momcare_platform.core.users.models import User

pytestmark = pytest.mark.django_db


def detail_url(staff_id):
    return f"/api/staff/{staff_id}/"


def assignment_status_url(staff_id):
    return f"/api/staff/{staff_id}/assignment-status/"


def deactivate_url(staff_id):
    return f"/api/staff/{staff_id}/deactivate/"


def reactivate_url(staff_id):
    return f"/api/staff/{staff_id}/reactivate/"


def patch(client, headers, url, **fields):
    return client.patch(url, data=json.dumps(fields), content_type="application/json", **headers)


def post(client, headers, url, **fields):
    return client.post(url, data=json.dumps(fields), content_type="application/json", **headers)


def make_manager_and_staff(make_hospital, make_staff, hospital_name, manager_email, staff_email):
    """A location managed by `manager`, with `subordinate` (a separate staff
    member) assigned to that same location."""
    hospital = make_hospital(hospital_name)
    manager = make_staff(hospital.org, settings.ROLE_NURSE, manager_email)
    location = Location.objects.create(organization=hospital.org, name="Their Branch", location_manager=manager)
    subordinate = make_staff(hospital.org, settings.ROLE_NURSE, staff_email)
    subordinate.locations.add(location)
    return hospital, manager, subordinate, location


# --- Update ------------------------------------------------------------


def test_hospital_admin_can_change_a_staff_members_role_and_locations(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Admin Update Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@adminupdate.test")
    new_location = Location.objects.create(
        organization=hospital.org,
        name="Second Branch",
        location_manager=hospital.admin,
    )

    response = patch(
        client,
        auth(hospital.admin.email),
        detail_url(nurse.staff.id),
        role_code=settings.ROLE_CARE_MANAGER,
        locations=[str(new_location.id)],
    )

    assert response.status_code == 200, response.content
    assert response.json()["role_code"] == settings.ROLE_CARE_MANAGER
    nurse.refresh_from_db()
    assert nurse.role_code == settings.ROLE_CARE_MANAGER
    assert list(nurse.locations.values_list("id", flat=True)) == [new_location.id]


def test_a_manager_can_update_staff_assigned_to_their_own_location(client, make_hospital, make_staff, auth):
    _, manager, subordinate, location = make_manager_and_staff(
        make_hospital, make_staff, "Manager Update Hospital", "manager@managerupdate.test", "sub@managerupdate.test"
    )

    response = patch(
        client,
        auth(manager.email),
        detail_url(subordinate.staff.id),
        specialty="Postnatal care",
    )

    assert response.status_code == 200, response.content
    subordinate.staff.refresh_from_db()
    assert subordinate.staff.specialty == "Postnatal care"


def test_a_manager_cannot_reassign_staff_to_a_location_they_dont_manage(client, make_hospital, make_staff, auth):
    hospital, manager, subordinate, _location = make_manager_and_staff(
        make_hospital,
        make_staff,
        "Manager Overreach Update Hospital",
        "manager@managerupdate2.test",
        "sub@managerupdate2.test",
    )
    someone_elses_location = Location.objects.create(
        organization=hospital.org,
        name="Not Theirs",
        location_manager=hospital.admin,
    )

    response = patch(
        client,
        auth(manager.email),
        detail_url(subordinate.staff.id),
        locations=[str(someone_elses_location.id)],
    )

    assert response.status_code == 400
    assert "locations" in response.json()


def test_a_manager_cannot_promote_anyone_to_hospital_admin(client, make_hospital, make_staff, auth):
    _, manager, subordinate, _location = make_manager_and_staff(
        make_hospital,
        make_staff,
        "Manager Escalation Hospital",
        "manager@managerescalation.test",
        "sub@managerescalation.test",
    )

    response = patch(
        client,
        auth(manager.email),
        detail_url(subordinate.staff.id),
        role_code=settings.ROLE_HOSPITAL_ADMIN,
    )

    assert response.status_code == 400
    assert "role_code" in response.json()


def test_a_manager_cannot_update_staff_at_a_location_they_dont_manage(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Unrelated Manager Hospital")
    manager = make_staff(hospital.org, settings.ROLE_NURSE, "manager@unrelatedmanager.test")
    Location.objects.create(organization=hospital.org, name="Their Own Branch", location_manager=manager)
    unrelated = make_staff(hospital.org, settings.ROLE_NURSE, "unrelated@unrelatedmanager.test")

    response = patch(client, auth(manager.email), detail_url(unrelated.staff.id), specialty="Something")

    assert response.status_code == 403


def test_a_staff_member_can_still_only_update_their_own_credentialing_fields(client, make_hospital, make_staff, auth):
    """Self-service stays narrow -- a nurse editing her own profile cannot
    grant herself a new role or reassign her own locations."""
    hospital = make_hospital("Self Service Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@selfservice2.test")

    response = patch(
        client,
        auth(nurse.email),
        detail_url(nurse.staff.id),
        specialty="Obstetrics",
        role_code=settings.ROLE_HOSPITAL_ADMIN,
    )

    assert response.status_code == 200, response.content
    nurse.refresh_from_db()
    assert nurse.staff.specialty == "Obstetrics"
    assert nurse.role_code == settings.ROLE_NURSE  # role_code silently ignored, not escalated


def test_assignment_status_reflects_a_staff_members_active_caseload(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Assignment Status Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@assignmentstatusstaff.test")
    patient = onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
        pregnancy_data={"lmp": datetime.date(2026, 1, 1), "provider": nurse.staff},
    )

    response = client.get(assignment_status_url(nurse.staff.id), **auth(hospital.admin.email))

    assert response.status_code == 200
    body = response.json()
    assert body["has_active_patients"] is True
    assert body["active_patient_count"] == 1
    assert patient.id  # sanity: the patient really was created


def test_deactivate_is_blocked_while_a_pregnancy_is_assigned(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Blocked Deactivate Staff Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@blockeddeactivate.test")
    onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
        pregnancy_data={"lmp": datetime.date(2026, 1, 1), "provider": nurse.staff},
    )

    response = post(client, auth(hospital.admin.email), deactivate_url(nurse.staff.id))

    assert response.status_code == 400
    nurse.staff.refresh_from_db()
    assert nurse.staff.is_active is True


def test_deactivate_is_blocked_while_assigned_as_nurse_too(client, make_hospital, make_staff, auth):
    """Deactivation is blocked for a supporting role, not just the lead —
    the nurse slot counts towards caseload exactly as provider does."""
    hospital = make_hospital("Care Team Block Hospital")
    lead = make_staff(hospital.org, settings.ROLE_PROVIDER, "lead@careteamblock.test")
    supporting = make_staff(hospital.org, settings.ROLE_NURSE, "supporting@careteamblock.test")
    onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
        pregnancy_data={
            "lmp": datetime.date(2026, 1, 1),
            "provider": lead.staff,
            "nurse": supporting.staff,
        },
    )

    response = post(client, auth(hospital.admin.email), deactivate_url(supporting.staff.id))

    assert response.status_code == 400


def test_hospital_admin_can_deactivate_and_reactivate_a_staff_member(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Admin Deactivate Staff Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@admindeactivatestaff.test")

    deactivated = post(client, auth(hospital.admin.email), deactivate_url(nurse.staff.id), reason="Left the team.")
    assert deactivated.status_code == 200, deactivated.content
    nurse.staff.refresh_from_db()
    assert nurse.staff.is_active is False

    reactivated = post(client, auth(hospital.admin.email), reactivate_url(nurse.staff.id))
    assert reactivated.status_code == 200, reactivated.content
    nurse.staff.refresh_from_db()
    assert nurse.staff.is_active is True


def test_a_manager_can_deactivate_staff_assigned_to_their_own_location(client, make_hospital, make_staff, auth):
    _, manager, subordinate, _location = make_manager_and_staff(
        make_hospital,
        make_staff,
        "Manager Deactivate Staff Hospital",
        "manager@managerdeactivatestaff.test",
        "sub@managerdeactivatestaff.test",
    )

    response = post(client, auth(manager.email), deactivate_url(subordinate.staff.id))

    assert response.status_code == 200, response.content


def test_a_staff_member_with_no_authority_cannot_deactivate_anyone(client, make_hospital, make_staff, auth):
    hospital = make_hospital("No Authority Deactivate Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@noauthoritydeactivate.test")
    target = make_staff(hospital.org, settings.ROLE_NURSE, "target@noauthoritydeactivate.test")

    response = post(client, auth(nurse.email), deactivate_url(target.staff.id))

    assert response.status_code == 403


# --- Delete --------------------------------------------------------------


def test_deleting_an_active_staff_member_is_refused(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Delete Active Staff Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@deleteactivestaff.test")

    response = client.delete(detail_url(nurse.staff.id), **auth(hospital.admin.email))

    assert response.status_code == 400
    assert Staff.objects.filter(pk=nurse.staff.id).exists()


def test_hospital_admin_can_delete_a_deactivated_staff_member_with_no_history(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Delete Clean Staff Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@deletecleanstaff.test")
    nurse.staff.deactivate()
    user_id = nurse.id

    response = client.delete(detail_url(nurse.staff.id), **auth(hospital.admin.email))

    assert response.status_code == 204
    assert not User.objects.filter(pk=user_id).exists()


def test_a_manager_cannot_delete_staff(client, make_hospital, make_staff, auth):
    _, manager, subordinate, _location = make_manager_and_staff(
        make_hospital,
        make_staff,
        "Manager Delete Staff Hospital",
        "manager@managerdeletestaff.test",
        "sub@managerdeletestaff.test",
    )
    subordinate.staff.deactivate()

    response = client.delete(detail_url(subordinate.staff.id), **auth(manager.email))

    assert response.status_code == 403
    assert Staff.objects.filter(pk=subordinate.staff.id).exists()


def test_deleting_a_staff_member_with_clinical_history_is_refused_not_500(client, make_hospital, make_staff, auth):
    """A deactivated pregnancy's ``provider`` (PROTECT) survives the
    staff member's own deactivation -- history is never deleted either."""
    hospital = make_hospital("Delete History Staff Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@deletehistorystaff.test")
    patient = onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
        pregnancy_data={
            "lmp": datetime.date(2026, 1, 1),
            "provider": nurse.staff,
            "status": "delivered",
        },
    )
    assert patient.pregnancies.get().provider_id == nurse.staff.id
    nurse.staff.deactivate()

    response = client.delete(detail_url(nurse.staff.id), **auth(hospital.admin.email))

    assert response.status_code == 400
    assert Staff.objects.filter(pk=nurse.staff.id).exists()


# --- Cross-tenant: 404, never 403 -----------------------------------------


def test_updating_another_hospitals_staff_404s(client, make_hospital, make_staff, auth):
    ours = make_hospital("Ours Staff Hospital")
    theirs = make_hospital("Theirs Staff Hospital")
    theirs_nurse = make_staff(theirs.org, settings.ROLE_NURSE, "nurse@theirsstaff.test")

    response = patch(
        client,
        auth(ours.admin.email),
        detail_url(theirs_nurse.staff.id),
        specialty="Should not resolve",
    )

    assert response.status_code == 404
