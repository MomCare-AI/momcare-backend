"""A staff member's credentialing profile — qualifications, specialty,
registration, experience. Self-reported, writable by the person themselves
or their hospital_admin, never by a colleague.
"""

import json

import pytest
from django.conf import settings

pytestmark = pytest.mark.django_db


def detail_url(staff_id):
    return f"/api/staff/{staff_id}/"


def test_a_staff_member_can_update_their_own_profile(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Self Edit Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@selfedit.test")

    response = client.patch(
        detail_url(nurse.staff.id),
        data=json.dumps({
            "qualifications": "BSN, RN",
            "specialty": "Obstetric nursing",
            "registration_number": "PNC-88213",
            "registration_authority": "Pakistan Nursing Council",
        }),
        content_type="application/json",
        **auth(nurse.email),
    )

    assert response.status_code == 200, response.content
    nurse.staff.refresh_from_db()
    assert nurse.staff.qualifications == "BSN, RN"
    assert nurse.staff.specialty == "Obstetric nursing"
    assert nurse.staff.registration_number == "PNC-88213"


def test_a_hospital_admin_can_update_anyones_profile(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Admin Edit Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doctor@adminedit.test")

    response = client.patch(
        detail_url(doctor.staff.id),
        data=json.dumps({"qualifications": "MBBS, FCPS (Gynae & Obs)"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200, response.content
    doctor.staff.refresh_from_db()
    assert doctor.staff.qualifications == "MBBS, FCPS (Gynae & Obs)"


def test_a_colleague_cannot_edit_someone_elses_profile(client, make_hospital, make_staff, auth):
    """The important boundary: holding a clinical role on the same team is
    not itself permission to edit a colleague's credentials."""
    hospital = make_hospital("Colleague Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@colleague.test")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doctor@colleague.test")

    response = client.patch(
        detail_url(doctor.staff.id),
        data=json.dumps({"qualifications": "Forged credential"}),
        content_type="application/json",
        **auth(nurse.email),
    )

    assert response.status_code == 403
    doctor.staff.refresh_from_db()
    assert doctor.staff.qualifications == ""


def test_practicing_since_derives_years_of_experience(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Experience Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doctor@experience.test")

    client.patch(
        detail_url(doctor.staff.id),
        data=json.dumps({"practicing_since": "2016-01-01"}),
        content_type="application/json",
        **auth(doctor.email),
    )

    response = client.get(detail_url(doctor.staff.id), **auth(hospital.admin.email))

    assert response.status_code == 200
    assert response.json()["years_of_experience"] >= 9


def test_a_staff_member_cannot_change_their_own_role_or_employee_id(
    client, make_hospital, make_staff, auth,
):
    """The write serializer only exposes credentialing fields - proven here
    by sending a role_code the API doesn't even accept, not just trusting
    the serializer's field list."""
    hospital = make_hospital("No Escalation Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@noescalation.test")
    original_employee_id = nurse.staff.employee_id

    response = client.patch(
        detail_url(nurse.staff.id),
        data=json.dumps({"employee_id": "HACKED-001", "role_code": settings.ROLE_HOSPITAL_ADMIN}),
        content_type="application/json",
        **auth(nurse.email),
    )

    assert response.status_code == 200, response.content
    nurse.staff.refresh_from_db()
    nurse.refresh_from_db()
    assert nurse.staff.employee_id == original_employee_id
    assert nurse.role_code == settings.ROLE_NURSE


def test_staff_in_another_hospital_cannot_be_reached(client, make_hospital, make_staff, auth):
    hospital_a = make_hospital("Isolation Staff A")
    hospital_b = make_hospital("Isolation Staff B")
    outsider = make_staff(hospital_b.org, settings.ROLE_NURSE, "nurse@isolationb.test")

    response = client.patch(
        detail_url(outsider.staff.id),
        data=json.dumps({"specialty": "Should not land"}),
        content_type="application/json",
        **auth(hospital_a.admin.email),
    )

    assert response.status_code == 404
    outsider.staff.refresh_from_db()
    assert outsider.staff.specialty == ""


def test_the_staff_list_carries_the_new_credentialing_fields(
    client, make_hospital, make_staff, auth,
):
    hospital = make_hospital("List Fields Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doctor@listfields.test")
    doctor.staff.specialty = "Maternal-Fetal Medicine"
    doctor.staff.save(update_fields=["specialty", "updated_at"])

    response = client.get("/api/staff/", **auth(hospital.admin.email))

    row = next(r for r in response.json() if r["id"] == str(doctor.staff.id))
    assert row["specialty"] == "Maternal-Fetal Medicine"
    assert row["years_of_experience"] is None
