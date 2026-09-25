"""``GET /api/staff/<id>/audit-report/`` -- access control and the
``period`` query param. KPI correctness itself is covered at the service
layer in ``test_audit_report.py``; this file is about who may see whose
report, and the API's shape around ``period``.
"""

import pytest
from django.conf import settings

from momcare_platform.core.locations.models import Location

pytestmark = pytest.mark.django_db


def audit_report_url(staff_id):
    return f"/api/staff/{staff_id}/audit-report/"


# ── Access control ───────────────────────────────────────────────────────


def test_staff_member_can_view_their_own_report(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Self Audit Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@selfaudit.test")

    response = client.get(audit_report_url(nurse.staff.id), **auth(nurse.email))

    assert response.status_code == 200
    assert response.json()["staff"]["id"] == str(nurse.staff.id)


def test_a_staff_member_cannot_view_another_staff_members_report(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Cross Staff Audit Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@crossaudit.test")
    other_nurse = make_staff(hospital.org, settings.ROLE_NURSE, "other@crossaudit.test")

    response = client.get(audit_report_url(other_nurse.staff.id), **auth(nurse.email))

    assert response.status_code == 403


def test_hospital_admin_can_view_any_staff_members_report(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Admin Audit Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@adminaudit.test")

    response = client.get(audit_report_url(nurse.staff.id), **auth(hospital.admin.email))

    assert response.status_code == 200


def test_location_manager_can_view_a_staff_members_report_at_their_location(
    client,
    make_hospital,
    make_staff,
    auth,
):
    hospital = make_hospital("Manager Audit Hospital")
    manager = make_staff(hospital.org, settings.ROLE_CARE_MANAGER, "manager@manageraudit.test")
    location = Location.objects.create(
        organization=hospital.org,
        name="Managed Branch",
        location_manager=manager,
    )
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@manageraudit.test")
    nurse.locations.add(location)

    response = client.get(audit_report_url(nurse.staff.id), **auth(manager.email))

    assert response.status_code == 200


def test_location_manager_cannot_view_a_staff_members_report_outside_their_location(
    client,
    make_hospital,
    make_staff,
    auth,
):
    hospital = make_hospital("Manager Outside Audit Hospital")
    manager = make_staff(hospital.org, settings.ROLE_CARE_MANAGER, "manager@manageroutside.test")
    Location.objects.create(organization=hospital.org, name="Managed Branch", location_manager=manager)
    other_location = Location.objects.create(
        organization=hospital.org,
        name="Unmanaged Branch",
        location_manager=hospital.admin,
    )
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@manageroutside.test")
    nurse.locations.add(other_location)

    response = client.get(audit_report_url(nurse.staff.id), **auth(manager.email))

    assert response.status_code == 403


def test_another_hospitals_staff_id_resolves_to_404(client, make_hospital, make_staff, auth):
    alpha = make_hospital("Alpha Audit Isolation")
    beta = make_hospital("Beta Audit Isolation")
    beta_nurse = make_staff(beta.org, settings.ROLE_NURSE, "nurse@betaauditisolation.test")

    response = client.get(audit_report_url(beta_nurse.staff.id), **auth(alpha.admin.email))

    assert response.status_code == 404


# ── period query param ───────────────────────────────────────────────────


def test_period_defaults_to_month_when_omitted(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Default Period Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@defaultperiod.test")

    response = client.get(audit_report_url(nurse.staff.id), **auth(nurse.email))

    assert response.json()["period"]["code"] == "month"


def test_an_explicit_valid_period_is_honored(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Explicit Period Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@explicitperiod.test")

    response = client.get(f"{audit_report_url(nurse.staff.id)}?period=week", **auth(nurse.email))

    assert response.status_code == 200
    assert response.json()["period"]["code"] == "week"


def test_an_unknown_period_is_rejected(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Unknown Period Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@unknownperiod.test")

    response = client.get(f"{audit_report_url(nurse.staff.id)}?period=fortnight", **auth(nurse.email))

    assert response.status_code == 400
