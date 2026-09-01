"""Care-team endpoints — supporting members alongside Pregnancy.assigned_staff.

Write access is hospital_admin only for now (see the view's own docstring
for why care_manager's boundary is deliberately not implemented yet).
Everyone on hospital staff can read.
"""

import json
from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from momcare_platform.core.patients.models import CareTeamMembership, Consent
from momcare_platform.core.patients.services import enrol_patient

pytestmark = pytest.mark.django_db


@pytest.fixture
def pregnancy_for(db):
    def _make(hospital, *, first_name="Ayesha", weeks_pregnant=20):
        lmp = timezone.now().date() - timedelta(weeks=weeks_pregnant)
        patient = enrol_patient(
            organization=hospital.org,
            recorded_by=hospital.admin,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
            pregnancy_data={"lmp": lmp},
            consent={"status": Consent.STATUS_GRANTED},
        )
        return patient.current_pregnancy

    return _make


def care_team_url(patient_id, pregnancy_id):
    return f"/api/patients/{patient_id}/pregnancies/{pregnancy_id}/care-team/"


def end_url(patient_id, pregnancy_id, membership_id):
    return f"/api/patients/{patient_id}/pregnancies/{pregnancy_id}/care-team/{membership_id}/end/"


def test_a_hospital_admin_can_add_a_care_team_member(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    hospital = make_hospital("Add Member Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@addmember.test")
    pregnancy = pregnancy_for(hospital)

    response = client.post(
        care_team_url(pregnancy.patient_id, pregnancy.id),
        data=json.dumps({"staff": str(nurse.staff.id), "role": "nurse"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201, response.content
    data = response.json()
    assert data["role"] == "nurse"
    assert data["is_active"] is True
    assert data["ended_at"] is None
    assert CareTeamMembership.objects.count() == 1


@pytest.mark.parametrize("role", [settings.ROLE_PROVIDER, settings.ROLE_NURSE, settings.ROLE_CARE_MANAGER])
def test_non_admins_cannot_add_a_care_team_member(
    client, make_hospital, make_staff, auth, pregnancy_for, role,
):
    """The plan's own recommendation was admin + care_manager (own cases) -
    deliberately not implemented until that boundary is actually decided.
    Everyone who isn't hospital_admin is denied for now, care_manager
    included."""
    hospital = make_hospital("No Self Assign Hospital")
    clinician = make_staff(hospital.org, role, f"acting-{role}@noselfassign.test")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "target-nurse@noselfassign.test")
    pregnancy = pregnancy_for(hospital)

    response = client.post(
        care_team_url(pregnancy.patient_id, pregnancy.id),
        data=json.dumps({"staff": str(nurse.staff.id), "role": "nurse"}),
        content_type="application/json",
        **auth(clinician.email),
    )

    assert response.status_code == 403


def test_everyone_on_hospital_staff_can_read_the_care_team(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    hospital = make_hospital("Read Access Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@readaccess.test")
    pregnancy = pregnancy_for(hospital)
    CareTeamMembership.objects.create(pregnancy=pregnancy, staff=nurse.staff, role="nurse")

    response = client.get(
        care_team_url(pregnancy.patient_id, pregnancy.id),
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["staff_name"] == nurse.get_full_name()


def test_an_admin_cannot_assign_a_staff_member_from_another_hospital(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    """OrganizationStaffField's own job - reused here, not reinvented."""
    hospital_a = make_hospital("Hospital A CareTeam")
    hospital_b = make_hospital("Hospital B CareTeam")
    outside_nurse = make_staff(hospital_b.org, settings.ROLE_NURSE, "nurse@hospitalb.test")
    pregnancy = pregnancy_for(hospital_a)

    response = client.post(
        care_team_url(pregnancy.patient_id, pregnancy.id),
        data=json.dumps({"staff": str(outside_nurse.staff.id), "role": "nurse"}),
        content_type="application/json",
        **auth(hospital_a.admin.email),
    )

    assert response.status_code == 400
    assert CareTeamMembership.objects.count() == 0


def test_a_deactivated_staff_member_cannot_be_assigned_through_the_api(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    hospital = make_hospital("Deactivated API Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@deactivatedapi.test")
    nurse.staff.deactivate(reason="Left before this was attempted")
    pregnancy = pregnancy_for(hospital)

    response = client.post(
        care_team_url(pregnancy.patient_id, pregnancy.id),
        data=json.dumps({"staff": str(nurse.staff.id), "role": "nurse"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    # OrganizationStaffField already filters is_active=True, so this is
    # rejected as "not a valid choice" before the model's own guard is ever
    # reached - both layers independently refuse it.
    assert response.status_code == 400
    assert CareTeamMembership.objects.count() == 0


def test_a_hospital_admin_can_end_a_membership(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    hospital = make_hospital("End Membership Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@endmembership.test")
    pregnancy = pregnancy_for(hospital)
    membership = CareTeamMembership.objects.create(pregnancy=pregnancy, staff=nurse.staff, role="nurse")

    response = client.post(
        end_url(pregnancy.patient_id, pregnancy.id, membership.id),
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200, response.content
    membership.refresh_from_db()
    assert membership.is_active is False
    assert membership.ended_at is not None
    assert membership.ended_by == hospital.admin


def test_a_patient_in_another_hospital_returns_404_not_403(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    """Cross-tenant reads return 404, never 403 - a 403 would confirm the
    record exists elsewhere. Same rule as everywhere else in this API."""
    hospital_a = make_hospital("Isolation Hospital A")
    hospital_b = make_hospital("Isolation Hospital B")
    pregnancy = pregnancy_for(hospital_b)

    response = client.get(
        care_team_url(pregnancy.patient_id, pregnancy.id),
        **auth(hospital_a.admin.email),
    )

    assert response.status_code == 404


# ── "?assigned_to=me" on GET /api/patients/ ─────────────────────────────


def patients_url(**params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"/api/patients/?{query}" if query else "/api/patients/"


def test_a_providers_my_patients_includes_both_lead_and_co_provider_cases(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    """The fix from the critique pass: assigned_staff alone would have made
    a genuine co-provider invisible in their own workspace."""
    hospital = make_hospital("Provider My Patients Hospital")
    lead = make_staff(hospital.org, settings.ROLE_PROVIDER, "lead@providermine.test")
    co_provider = make_staff(hospital.org, settings.ROLE_PROVIDER, "co@providermine.test")
    stranger = make_staff(hospital.org, settings.ROLE_PROVIDER, "stranger@providermine.test")

    led_pregnancy = pregnancy_for(hospital, first_name="LedByLead")
    led_pregnancy.assigned_staff = lead.staff
    led_pregnancy.save(update_fields=["assigned_staff", "updated_at"])

    supported_pregnancy = pregnancy_for(hospital, first_name="SupportedByCoProvider")
    CareTeamMembership.objects.create(pregnancy=supported_pregnancy, staff=co_provider.staff, role="provider")

    pregnancy_for(hospital, first_name="NotMine")  # neither lead nor co-provider on this one

    response = client.get(patients_url(assigned_to="me"), **auth(co_provider.email))

    assert response.status_code == 200
    names = {row["full_name"] for row in response.json()["results"]}
    assert names == {"SupportedByCoProvider Bibi"}

    response = client.get(patients_url(assigned_to="me"), **auth(lead.email))
    names = {row["full_name"] for row in response.json()["results"]}
    assert names == {"LedByLead Bibi"}

    response = client.get(patients_url(assigned_to="me"), **auth(stranger.email))
    assert response.json()["results"] == []


def test_a_nurses_my_patients_is_membership_only(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    hospital = make_hospital("Nurse My Patients Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@nursemine.test")
    other_nurse = make_staff(hospital.org, settings.ROLE_NURSE, "other@nursemine.test")

    assigned = pregnancy_for(hospital, first_name="NurseAssigned")
    CareTeamMembership.objects.create(pregnancy=assigned, staff=nurse.staff, role="nurse")
    pregnancy_for(hospital, first_name="NotThisNurse")

    response = client.get(patients_url(assigned_to="me"), **auth(nurse.email))
    names = {row["full_name"] for row in response.json()["results"]}
    assert names == {"NurseAssigned Bibi"}

    response = client.get(patients_url(assigned_to="me"), **auth(other_nurse.email))
    assert response.json()["results"] == []


def test_an_ended_membership_no_longer_counts_toward_my_patients(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    hospital = make_hospital("Ended Membership Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@endedmine.test")
    pregnancy = pregnancy_for(hospital)
    membership = CareTeamMembership.objects.create(pregnancy=pregnancy, staff=nurse.staff, role="nurse")
    membership.end()

    response = client.get(patients_url(assigned_to="me"), **auth(nurse.email))

    assert response.json()["results"] == []


def test_hospital_admin_assigned_to_me_returns_empty_not_everyone(
    client, make_hospital, auth, pregnancy_for,
):
    """"My patients" isn't a concept that applies to an admin - an honest
    empty result, not silently falling back to the whole hospital under a
    label that would be wrong for this role."""
    hospital = make_hospital("Admin Empty Hospital")
    pregnancy_for(hospital)

    response = client.get(patients_url(assigned_to="me"), **auth(hospital.admin.email))

    assert response.status_code == 200
    assert response.json()["results"] == []


def test_without_the_query_param_everyone_still_sees_the_full_hospital_list(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    """The default, unfiltered behaviour every existing page already relies
    on must not change just because this filter was added."""
    hospital = make_hospital("Unfiltered Hospital")
    make_staff(hospital.org, settings.ROLE_PROVIDER, "someone@unfiltered.test")
    pregnancy_for(hospital, first_name="First")
    pregnancy_for(hospital, first_name="Second")

    response = client.get(patients_url(), **auth(hospital.admin.email))

    assert response.status_code == 200
    assert len(response.json()["results"]) == 2
