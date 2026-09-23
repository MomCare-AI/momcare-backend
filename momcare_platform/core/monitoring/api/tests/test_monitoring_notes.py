"""``/patients/<id>/monitoring/notes/`` and ``/monitoring-notes/<id>/``."""

import json

import pytest
from django.conf import settings
from django.utils import timezone

from momcare_platform.core.monitoring.models import ClinicalTag, MonitoringNote
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db


@pytest.fixture
def patient_for(make_hospital):
    def _make(hospital, first_name="Ayesha"):
        return onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
            pregnancy_data={"lmp": timezone.now().date()},
        )

    return _make


def notes_url(patient_id):
    return f"/api/patients/{patient_id}/monitoring/notes/"


def note_detail_url(note_id):
    return f"/api/monitoring-notes/{note_id}/"


def make_note(patient, added_by, text="A note.", tags=None):
    note = MonitoringNote.objects.create(
        patient=patient,
        pregnancy=patient.current_pregnancy,
        note=text,
        added_by=added_by,
    )
    if tags:
        note.tags.set(tags)
    return note


def test_notes_list_is_scoped_to_the_patient(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Note List Hospital")
    patient = patient_for(hospital)
    other_patient = patient_for(hospital, "Sana")
    make_note(patient, hospital.admin, "For the right patient.")
    make_note(other_patient, hospital.admin, "For someone else.")

    response = client.get(notes_url(patient.id), **auth(hospital.admin.email))

    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_notes_can_be_searched_by_text(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Note Search Hospital")
    patient = patient_for(hospital)
    make_note(patient, hospital.admin, "Reports swelling in ankles.")
    make_note(patient, hospital.admin, "Routine check-in, nothing notable.")

    response = client.get(f"{notes_url(patient.id)}?search=swelling", **auth(hospital.admin.email))

    assert response.json()["count"] == 1
    assert "swelling" in response.json()["results"][0]["note"]


def test_notes_can_be_filtered_by_tag(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Note Tag Filter Hospital")
    patient = patient_for(hospital)
    tag = ClinicalTag.objects.create(name="Urgent", organization=hospital.org)
    make_note(patient, hospital.admin, "Flagged as urgent.", tags=[tag])
    make_note(patient, hospital.admin, "Not flagged.")

    response = client.get(f"{notes_url(patient.id)}?tag_id={tag.id}", **auth(hospital.admin.email))

    assert response.json()["count"] == 1


def test_author_can_edit_their_own_note_text(client, make_hospital, make_staff, patient_for, auth):
    hospital = make_hospital("Note Edit Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@noteedit.test")
    patient = patient_for(hospital)
    note = make_note(patient, nurse, "Original text.")

    response = client.patch(
        note_detail_url(note.id),
        data=json.dumps({"note": "Corrected text."}),
        content_type="application/json",
        **auth(nurse.email),
    )

    assert response.status_code == 200
    assert response.json()["note"] == "Corrected text."


def test_a_different_staff_member_cannot_edit_someone_elses_note(client, make_hospital, make_staff, patient_for, auth):
    hospital = make_hospital("Note Ownership Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@noteown.test")
    other_nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="other@noteown.test")
    patient = patient_for(hospital)
    note = make_note(patient, nurse, "Not yours.")

    response = client.patch(
        note_detail_url(note.id),
        data=json.dumps({"note": "Trying to edit."}),
        content_type="application/json",
        **auth(other_nurse.email),
    )

    assert response.status_code == 403


def test_updating_tags_input_replaces_the_tag_set(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Note Tag Update Hospital")
    patient = patient_for(hospital)
    old_tag = ClinicalTag.objects.create(name="Old Tag", organization=hospital.org)
    note = make_note(patient, hospital.admin, "Tag update test.", tags=[old_tag])

    response = client.patch(
        note_detail_url(note.id),
        data=json.dumps({"tags_input": [{"name": "New Tag"}]}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200
    names = [t["name"] for t in response.json()["tags"]]
    assert names == ["New Tag"]


def test_blank_note_text_is_rejected(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Blank Note Hospital")
    patient = patient_for(hospital)
    note = make_note(patient, hospital.admin)

    response = client.patch(
        note_detail_url(note.id),
        data=json.dumps({"note": "   "}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400


def test_author_can_delete_their_own_note(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Note Delete Hospital")
    patient = patient_for(hospital)
    note = make_note(patient, hospital.admin)

    response = client.delete(note_detail_url(note.id), **auth(hospital.admin.email))

    assert response.status_code == 204
    assert not MonitoringNote.objects.filter(id=note.id).exists()


def test_another_hospitals_note_resolves_to_404(client, make_hospital, patient_for, auth):
    alpha = make_hospital("Alpha Note Isolation")
    beta = make_hospital("Beta Note Isolation")
    beta_patient = patient_for(beta)
    beta_note = make_note(beta_patient, beta.admin)

    response = client.get(note_detail_url(beta_note.id), **auth(alpha.admin.email))

    assert response.status_code == 404
