"""Deactivating and reactivating a patient.

Soft-deactivation only — the same rule Locations and Staff already follow.
A patient's clinical record is never physically deleted.
"""

import json

import pytest
from django.conf import settings

from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db

PATIENTS = "/api/patients/"


@pytest.fixture
def patient_for(db):
    def _make(hospital, *, first_name="Ayesha"):
        return onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
        )

    return _make


def post(client, headers, url, body=None):
    return client.post(url, data=json.dumps(body or {}), content_type="application/json", **headers)


def test_deactivating_a_patient_records_who_and_why(client, make_hospital, auth, patient_for):
    hospital = make_hospital("Deactivate Patient Hospital")
    patient = patient_for(hospital)

    response = post(
        client,
        auth(hospital.admin.email),
        f"{PATIENTS}{patient.id}/deactivate/",
        {"reason": "Moved out of the catchment area"},
    )

    assert response.status_code == 200
    assert response.json()["is_active"] is False
    patient.refresh_from_db()
    assert patient.is_active is False
    assert patient.deactivation_reason == "Moved out of the catchment area"
    assert patient.deactivated_at is not None
    assert patient.deactivated_by == hospital.admin


def test_reactivating_clears_the_deactivation(client, make_hospital, auth, patient_for):
    hospital = make_hospital("Reactivate Patient Hospital")
    patient = patient_for(hospital)
    headers = auth(hospital.admin.email)
    post(client, headers, f"{PATIENTS}{patient.id}/deactivate/", {"reason": "Left"})

    response = post(client, headers, f"{PATIENTS}{patient.id}/reactivate/")

    assert response.status_code == 200
    assert response.json()["is_active"] is True
    patient.refresh_from_db()
    assert patient.is_active is True
    assert patient.deactivated_at is None
    assert patient.deactivation_reason == ""


def test_a_deactivated_patient_keeps_her_clinical_record(client, make_hospital, auth, patient_for):
    """Deactivation is not deletion — the record is still fully readable."""
    hospital = make_hospital("Record Survives Hospital")
    patient = patient_for(hospital)
    headers = auth(hospital.admin.email)
    post(client, headers, f"{PATIENTS}{patient.id}/deactivate/")

    body = client.get(f"{PATIENTS}{patient.id}/", **headers).json()

    assert body["is_active"] is False
    assert body["full_name"] == "Ayesha Bibi"


def test_another_hospital_cannot_deactivate_our_patient(client, make_hospital, auth, patient_for):
    hospital = make_hospital("Owner Deactivate Hospital")
    rival = make_hospital("Rival Deactivate Hospital")
    patient = patient_for(hospital)

    response = post(client, auth(rival.admin.email), f"{PATIENTS}{patient.id}/deactivate/")

    assert response.status_code == 404
    patient.refresh_from_db()
    assert patient.is_active is True


def test_another_hospital_cannot_reactivate_our_patient(client, make_hospital, auth, patient_for):
    hospital = make_hospital("Owner Reactivate Hospital")
    rival = make_hospital("Rival Reactivate Hospital")
    patient = patient_for(hospital)
    post(client, auth(hospital.admin.email), f"{PATIENTS}{patient.id}/deactivate/")

    response = post(client, auth(rival.admin.email), f"{PATIENTS}{patient.id}/reactivate/")

    assert response.status_code == 404
    patient.refresh_from_db()
    assert patient.is_active is False


# ── Permission: hospital_admin or care_manager only, matching Neuro_RPM's own
# MANAGE = IsAdmin | IsCareManager on the identical action. Found via a
# permission audit -- this view had inherited the base PatientScopedView's
# plain "any staff" permission, which is correct for read/create/update but
# was too broad here.
def test_a_care_manager_can_deactivate_a_patient(client, make_hospital, make_staff, auth, patient_for):
    hospital = make_hospital("Care Manager Deactivate Hospital")
    care_manager = make_staff(hospital.org, settings.ROLE_CARE_MANAGER, email="cm@deactivate.test")
    patient = patient_for(hospital)

    response = post(client, auth(care_manager.email), f"{PATIENTS}{patient.id}/deactivate/", {"reason": "Left"})

    assert response.status_code == 200
    patient.refresh_from_db()
    assert patient.is_active is False


def test_a_provider_cannot_deactivate_a_patient(client, make_hospital, make_staff, auth, patient_for):
    """Fault injection: Provider and Nurse are clinicians, but Neuro_RPM's
    own equivalent action still excludes them -- deactivation is an
    administrative/care-management call, not a clinical one."""
    hospital = make_hospital("Provider Cannot Deactivate Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, email="provider@deactivate.test")
    patient = patient_for(hospital)

    response = post(client, auth(provider.email), f"{PATIENTS}{patient.id}/deactivate/", {"reason": "Left"})

    assert response.status_code == 403
    patient.refresh_from_db()
    assert patient.is_active is True


def test_a_nurse_cannot_deactivate_a_patient(client, make_hospital, make_staff, auth, patient_for):
    hospital = make_hospital("Nurse Cannot Deactivate Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@deactivate.test")
    patient = patient_for(hospital)

    response = post(client, auth(nurse.email), f"{PATIENTS}{patient.id}/deactivate/", {"reason": "Left"})

    assert response.status_code == 403


def test_a_care_manager_can_reactivate_a_patient(client, make_hospital, make_staff, auth, patient_for):
    hospital = make_hospital("Care Manager Reactivate Hospital")
    care_manager = make_staff(hospital.org, settings.ROLE_CARE_MANAGER, email="cm@reactivate.test")
    patient = patient_for(hospital)
    post(client, auth(hospital.admin.email), f"{PATIENTS}{patient.id}/deactivate/")

    response = post(client, auth(care_manager.email), f"{PATIENTS}{patient.id}/reactivate/")

    assert response.status_code == 200
    patient.refresh_from_db()
    assert patient.is_active is True


def test_a_provider_cannot_reactivate_a_patient(client, make_hospital, make_staff, auth, patient_for):
    hospital = make_hospital("Provider Cannot Reactivate Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, email="provider@reactivate.test")
    patient = patient_for(hospital)
    post(client, auth(hospital.admin.email), f"{PATIENTS}{patient.id}/deactivate/")

    response = post(client, auth(provider.email), f"{PATIENTS}{patient.id}/reactivate/")

    assert response.status_code == 403
