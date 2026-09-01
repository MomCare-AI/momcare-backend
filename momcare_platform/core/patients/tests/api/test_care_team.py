"""Care-team endpoints — supporting members alongside Pregnancy.assigned_staff.

Write access: hospital_admin (org/location-wide), or a care_manager with an
active membership on this specific pregnancy — see the view module's
``_can_manage_care_team``. Provider and nurse are read-only. Everyone on
hospital staff can read.
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
    """Provider and nurse never get write access. A care_manager with no
    active membership on *this* pregnancy is denied too - see the
    care_manager-specific tests below for the case where they do hold one."""
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


def test_provider_and_nurse_cannot_end_a_membership(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    hospital = make_hospital("Non Admin End Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, "provider@nonadminend.test")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@nonadminend.test")
    target = make_staff(hospital.org, settings.ROLE_NURSE, "target@nonadminend.test")
    pregnancy = pregnancy_for(hospital)
    membership = CareTeamMembership.objects.create(pregnancy=pregnancy, staff=target.staff, role="nurse")

    for actor in (provider, nurse):
        response = client.post(
            end_url(pregnancy.patient_id, pregnancy.id, membership.id),
            **auth(actor.email),
        )
        assert response.status_code == 403

    membership.refresh_from_db()
    assert membership.is_active is True


# ── care_manager's pregnancy-scoped write access ────────────────────────


def test_an_active_care_manager_on_the_pregnancy_can_add_a_member(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    hospital = make_hospital("CM Add Hospital")
    manager = make_staff(hospital.org, settings.ROLE_CARE_MANAGER, "manager@cmadd.test")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@cmadd.test")
    pregnancy = pregnancy_for(hospital)
    CareTeamMembership.objects.create(pregnancy=pregnancy, staff=manager.staff, role="care_manager")

    response = client.post(
        care_team_url(pregnancy.patient_id, pregnancy.id),
        data=json.dumps({"staff": str(nurse.staff.id), "role": "nurse"}),
        content_type="application/json",
        **auth(manager.email),
    )

    assert response.status_code == 201, response.content


def test_an_active_care_manager_on_the_pregnancy_can_end_a_membership(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    hospital = make_hospital("CM End Hospital")
    manager = make_staff(hospital.org, settings.ROLE_CARE_MANAGER, "manager@cmend.test")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@cmend.test")
    pregnancy = pregnancy_for(hospital)
    CareTeamMembership.objects.create(pregnancy=pregnancy, staff=manager.staff, role="care_manager")
    target = CareTeamMembership.objects.create(pregnancy=pregnancy, staff=nurse.staff, role="nurse")

    response = client.post(
        end_url(pregnancy.patient_id, pregnancy.id, target.id),
        **auth(manager.email),
    )

    assert response.status_code == 200, response.content
    target.refresh_from_db()
    assert target.is_active is False


def test_a_care_manager_can_end_their_own_membership(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    hospital = make_hospital("CM Self End Hospital")
    manager = make_staff(hospital.org, settings.ROLE_CARE_MANAGER, "manager@cmselfend.test")
    pregnancy = pregnancy_for(hospital)
    own_membership = CareTeamMembership.objects.create(
        pregnancy=pregnancy, staff=manager.staff, role="care_manager",
    )

    response = client.post(
        end_url(pregnancy.patient_id, pregnancy.id, own_membership.id),
        **auth(manager.email),
    )

    assert response.status_code == 200, response.content
    own_membership.refresh_from_db()
    assert own_membership.is_active is False


def test_after_self_ending_the_care_managers_next_write_is_denied(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    """The important loophole check: self-removal must take effect
    immediately, not just cosmetically. Authorization is re-checked fresh
    against the database on every request, never cached from an earlier
    one - this proves it, rather than trusting the reasoning."""
    hospital = make_hospital("CM Loophole Hospital")
    manager = make_staff(hospital.org, settings.ROLE_CARE_MANAGER, "manager@cmloophole.test")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@cmloophole.test")
    pregnancy = pregnancy_for(hospital)
    own_membership = CareTeamMembership.objects.create(
        pregnancy=pregnancy, staff=manager.staff, role="care_manager",
    )
    client.post(
        end_url(pregnancy.patient_id, pregnancy.id, own_membership.id),
        **auth(manager.email),
    )

    response = client.post(
        care_team_url(pregnancy.patient_id, pregnancy.id),
        data=json.dumps({"staff": str(nurse.staff.id), "role": "nurse"}),
        content_type="application/json",
        **auth(manager.email),
    )

    assert response.status_code == 403


def test_a_care_manager_can_add_another_care_manager_to_the_same_pregnancy(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    """The explicit delegation decision: case-scoped, not organization-wide.
    The newly added care_manager gets the same pregnancy-scoped authority,
    proven here by having them immediately perform a write of their own."""
    hospital = make_hospital("CM Delegation Hospital")
    manager_a = make_staff(hospital.org, settings.ROLE_CARE_MANAGER, "a@cmdelegation.test")
    manager_b = make_staff(hospital.org, settings.ROLE_CARE_MANAGER, "b@cmdelegation.test")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@cmdelegation.test")
    pregnancy = pregnancy_for(hospital)
    CareTeamMembership.objects.create(pregnancy=pregnancy, staff=manager_a.staff, role="care_manager")

    response = client.post(
        care_team_url(pregnancy.patient_id, pregnancy.id),
        data=json.dumps({"staff": str(manager_b.staff.id), "role": "care_manager"}),
        content_type="application/json",
        **auth(manager_a.email),
    )
    assert response.status_code == 201, response.content

    response = client.post(
        care_team_url(pregnancy.patient_id, pregnancy.id),
        data=json.dumps({"staff": str(nurse.staff.id), "role": "nurse"}),
        content_type="application/json",
        **auth(manager_b.email),
    )
    assert response.status_code == 201, response.content


def test_a_care_manager_not_on_this_pregnancy_cannot_add_or_end(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    """Case-scoped, not role-wide: holding the care_manager role is not
    itself the permission - an active membership on *this* pregnancy is."""
    hospital = make_hospital("CM Elsewhere Hospital")
    manager = make_staff(hospital.org, settings.ROLE_CARE_MANAGER, "manager@cmelsewhere.test")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@cmelsewhere.test")
    pregnancy = pregnancy_for(hospital, first_name="NotManagersCase")
    target = CareTeamMembership.objects.create(pregnancy=pregnancy, staff=nurse.staff, role="nurse")

    response = client.post(
        care_team_url(pregnancy.patient_id, pregnancy.id),
        data=json.dumps({"staff": str(nurse.staff.id), "role": "nurse"}),
        content_type="application/json",
        **auth(manager.email),
    )
    assert response.status_code == 403

    response = client.post(
        end_url(pregnancy.patient_id, pregnancy.id, target.id),
        **auth(manager.email),
    )
    assert response.status_code == 403


def test_an_inactive_care_manager_membership_grants_no_write_access(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    """Holding a membership row that has already ended is the same as never
    having held one, for authorization purposes."""
    hospital = make_hospital("CM Inactive Hospital")
    manager = make_staff(hospital.org, settings.ROLE_CARE_MANAGER, "manager@cminactive.test")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@cminactive.test")
    pregnancy = pregnancy_for(hospital)
    ended = CareTeamMembership.objects.create(pregnancy=pregnancy, staff=manager.staff, role="care_manager")
    ended.end()

    response = client.post(
        care_team_url(pregnancy.patient_id, pregnancy.id),
        data=json.dumps({"staff": str(nurse.staff.id), "role": "nurse"}),
        content_type="application/json",
        **auth(manager.email),
    )

    assert response.status_code == 403


def test_a_care_manager_cannot_assign_staff_from_another_hospital(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    """OrganizationStaffField's scoping is actor-independent - proven again
    here for the care_manager write path, not just the admin one."""
    hospital_a = make_hospital("CM Cross Org A")
    hospital_b = make_hospital("CM Cross Org B")
    manager = make_staff(hospital_a.org, settings.ROLE_CARE_MANAGER, "manager@cmcrossorg.test")
    outsider = make_staff(hospital_b.org, settings.ROLE_NURSE, "outsider@cmcrossorg.test")
    pregnancy = pregnancy_for(hospital_a)
    CareTeamMembership.objects.create(pregnancy=pregnancy, staff=manager.staff, role="care_manager")

    response = client.post(
        care_team_url(pregnancy.patient_id, pregnancy.id),
        data=json.dumps({"staff": str(outsider.staff.id), "role": "nurse"}),
        content_type="application/json",
        **auth(manager.email),
    )

    assert response.status_code == 400


def test_ending_a_membership_from_another_pregnancy_returns_404(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    """The existing URL scoping (pregnancy__patient=patient,
    pregnancy_id=pregnancy_id) must keep holding under the new permission
    logic - an admin acting on the wrong pregnancy's URL for a real
    membership ID must not succeed."""
    hospital = make_hospital("Wrong Pregnancy Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@wrongpregnancy.test")
    real_pregnancy = pregnancy_for(hospital, first_name="Real")
    other_pregnancy = pregnancy_for(hospital, first_name="Other")
    membership = CareTeamMembership.objects.create(pregnancy=real_pregnancy, staff=nurse.staff, role="nurse")

    response = client.post(
        end_url(other_pregnancy.patient_id, other_pregnancy.id, membership.id),
        **auth(hospital.admin.email),
    )

    assert response.status_code == 404
    membership.refresh_from_db()
    assert membership.is_active is True


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
