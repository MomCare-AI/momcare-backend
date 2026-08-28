"""Clinical notes — the append-only record of what a clinician thought.

Split along the same line as acknowledging an alert: any hospital staff can
read (an admin may need one for a liability review), only a clinician can
write (this is a clinical judgement, not an admin task).
"""

import json
from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from momcare_platform.core.patients.models import ClinicalNote, Consent
from momcare_platform.core.patients.services import enrol_patient

pytestmark = pytest.mark.django_db


@pytest.fixture
def pregnancy_for(db):
    """Enrol a patient with an active pregnancy at a given hospital."""

    def _make(hospital, *, first_name="Ayesha", weeks_pregnant=28):
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


def notes_url(patient_id, pregnancy_id):
    return f"/api/patients/{patient_id}/pregnancies/{pregnancy_id}/notes/"


def test_a_provider_can_write_a_note(client, make_hospital, make_staff, auth, pregnancy_for):
    hospital = make_hospital("Note Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doctor@notehospital.test")
    pregnancy = pregnancy_for(hospital)

    response = client.post(
        notes_url(pregnancy.patient_id, pregnancy.id),
        data=json.dumps({"body": "BP trending up, advised more rest."}),
        content_type="application/json",
        **auth(doctor.email),
    )

    assert response.status_code == 201, response.content
    data = response.json()
    assert data["body"] == "BP trending up, advised more rest."
    assert data["author_name"] == doctor.get_full_name()
    assert data["author_role"] == settings.ROLE_PROVIDER
    assert ClinicalNote.objects.count() == 1


@pytest.mark.parametrize("role", [settings.ROLE_NURSE, settings.ROLE_CARE_MANAGER])
def test_nurses_and_care_managers_can_also_write_notes(
    client, make_hospital, make_staff, auth, pregnancy_for, role,
):
    hospital = make_hospital("Multi Clinician Hospital")
    clinician = make_staff(hospital.org, role, f"{role}@multiclinician.test")
    pregnancy = pregnancy_for(hospital)

    response = client.post(
        notes_url(pregnancy.patient_id, pregnancy.id),
        data=json.dumps({"body": "Checked in by phone, patient stable."}),
        content_type="application/json",
        **auth(clinician.email),
    )
    assert response.status_code == 201, response.content


def test_a_hospital_admin_cannot_write_a_note_but_can_read_them(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    """Same split as acknowledging an alert: an admin runs the hospital, not
    the patient's care — writing a clinical note is not their call."""
    hospital = make_hospital("Admin Boundary Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doc@adminboundary.test")
    pregnancy = pregnancy_for(hospital)

    ClinicalNote.objects.create(
        pregnancy=pregnancy,
        author=doctor.staff,
        body="Earlier note from the doctor.",
    )

    write = client.post(
        notes_url(pregnancy.patient_id, pregnancy.id),
        data=json.dumps({"body": "Admin trying to write a note."}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )
    assert write.status_code == 403

    read = client.get(notes_url(pregnancy.patient_id, pregnancy.id), **auth(hospital.admin.email))
    assert read.status_code == 200
    assert len(read.json()) == 1


def test_notes_are_returned_newest_first(client, make_hospital, make_staff, auth, pregnancy_for):
    hospital = make_hospital("Order Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doc@orderhospital.test")
    pregnancy = pregnancy_for(hospital)

    ClinicalNote.objects.create(pregnancy=pregnancy, author=doctor.staff, body="First note.")
    ClinicalNote.objects.create(pregnancy=pregnancy, author=doctor.staff, body="Second note.")

    response = client.get(notes_url(pregnancy.patient_id, pregnancy.id), **auth(doctor.email))
    bodies = [n["body"] for n in response.json()]
    assert bodies == ["Second note.", "First note."]


def test_an_empty_note_is_rejected(client, make_hospital, make_staff, auth, pregnancy_for):
    hospital = make_hospital("Empty Note Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doc@emptynote.test")
    pregnancy = pregnancy_for(hospital)

    response = client.post(
        notes_url(pregnancy.patient_id, pregnancy.id),
        data=json.dumps({"body": "   "}),
        content_type="application/json",
        **auth(doctor.email),
    )
    assert response.status_code == 400
    assert ClinicalNote.objects.count() == 0


def test_another_hospitals_notes_are_not_reachable(
    client, make_hospital, make_staff, auth, pregnancy_for,
):
    """A pregnancy at another hospital resolves to 404, never 403 — the same
    rule as every other cross-tenant boundary in this app."""
    hospital_a = make_hospital("Tenant A Notes Hospital")
    hospital_b = make_hospital("Tenant B Notes Hospital")
    doctor_b = make_staff(hospital_b.org, settings.ROLE_PROVIDER, "doc@tenantbnotes.test")
    pregnancy_a = pregnancy_for(hospital_a)

    response = client.get(
        notes_url(pregnancy_a.patient_id, pregnancy_a.id),
        **auth(doctor_b.email),
    )
    assert response.status_code == 404

    response = client.post(
        notes_url(pregnancy_a.patient_id, pregnancy_a.id),
        data=json.dumps({"body": "Should never land here."}),
        content_type="application/json",
        **auth(doctor_b.email),
    )
    assert response.status_code == 404
    assert ClinicalNote.objects.count() == 0
