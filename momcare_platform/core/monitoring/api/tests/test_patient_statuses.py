"""``/patients/<id>/statuses/`` and ``/patient-statuses/<id>/``."""

import json

import pytest
from django.conf import settings
from django.utils import timezone

from momcare_platform.core.monitoring.models import PatientStatus
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


def statuses_url(patient_id):
    return f"/api/patients/{patient_id}/statuses/"


def status_detail_url(status_id):
    return f"/api/patient-statuses/{status_id}/"


def post_json(client, url, data, **headers):
    return client.post(url, data=json.dumps(data), content_type="application/json", **headers)


def make_status(patient, added_by, name="Stable", color="#00ff00"):
    return PatientStatus.objects.create(
        patient=patient,
        pregnancy=patient.current_pregnancy,
        name=name,
        description="A status entry.",
        color=color,
        added_by=added_by,
    )


# ── Listing ───────────────────────────────────────────────────────────────


def test_statuses_list_is_scoped_to_the_patient(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Status List Hospital")
    patient = patient_for(hospital)
    other_patient = patient_for(hospital, "Sana")
    make_status(patient, hospital.admin, "For the right patient.")
    make_status(other_patient, hospital.admin, "For someone else.")

    response = client.get(statuses_url(patient.id), **auth(hospital.admin.email))

    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_most_recent_status_is_listed_first(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Status Order Hospital")
    patient = patient_for(hospital)
    make_status(patient, hospital.admin, "Waiting")
    make_status(patient, hospital.admin, "Stable")

    response = client.get(statuses_url(patient.id), **auth(hospital.admin.email))

    names = [row["name"] for row in response.json()["results"]]
    assert names == ["Stable", "Waiting"]


# ── Creating ──────────────────────────────────────────────────────────────


def test_any_hospital_staff_can_log_a_status(client, make_hospital, make_staff, patient_for, auth):
    hospital = make_hospital("Status Create Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@statuscreate.test")
    patient = patient_for(hospital)

    response = post_json(
        client,
        statuses_url(patient.id),
        {"name": "Critical", "description": "Needs urgent review", "color": "#ff0000"},
        **auth(nurse.email),
    )

    assert response.status_code == 201
    assert response.json()["name"] == "Critical"
    assert response.json()["patient"] == str(patient.id)


def test_status_name_can_be_free_text_not_in_any_catalogue(client, make_hospital, patient_for, auth):
    """No FK to StatusLabel -- see the model's own docstring on the
    deliberate decoupling from the catalogue."""
    hospital = make_hospital("Free Text Status Hospital")
    patient = patient_for(hospital)

    response = post_json(
        client,
        statuses_url(patient.id),
        {"name": "Made Up On The Spot", "description": "d", "color": "#abcdef"},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201


def test_blank_status_name_is_rejected(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Blank Status Name Hospital")
    patient = patient_for(hospital)

    response = post_json(
        client,
        statuses_url(patient.id),
        {"name": "   ", "description": "d", "color": "#abcdef"},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400


def test_invalid_hex_color_is_rejected(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Invalid Color Status Hospital")
    patient = patient_for(hospital)

    response = post_json(
        client,
        statuses_url(patient.id),
        {"name": "Stable", "description": "d", "color": "green"},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400


def test_description_is_required(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Description Required Hospital")
    patient = patient_for(hospital)

    response = post_json(
        client,
        statuses_url(patient.id),
        {"name": "Stable", "color": "#00ff00"},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400


def test_client_cannot_set_a_different_patient(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Patient Override Hospital")
    patient = patient_for(hospital)
    other_patient = patient_for(hospital, "Sana")

    response = post_json(
        client,
        statuses_url(patient.id),
        {"name": "Stable", "description": "d", "color": "#00ff00", "patient": str(other_patient.id)},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    assert response.json()["patient"] == str(patient.id)


def test_same_status_name_can_be_logged_again_later(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Recurring Status Hospital")
    patient = patient_for(hospital)
    make_status(patient, hospital.admin, "Stable")

    response = post_json(
        client,
        statuses_url(patient.id),
        {"name": "Stable", "description": "back to normal", "color": "#00ff00"},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    assert patient.statuses.filter(name="Stable").count() == 2


def test_another_hospitals_patient_resolves_to_404_on_create(client, make_hospital, patient_for, auth):
    alpha = make_hospital("Alpha Status Isolation")
    beta = make_hospital("Beta Status Isolation")
    beta_patient = patient_for(beta)

    response = post_json(
        client,
        statuses_url(beta_patient.id),
        {"name": "Stable", "description": "d", "color": "#00ff00"},
        **auth(alpha.admin.email),
    )

    assert response.status_code == 404


# ── Editing / deleting ────────────────────────────────────────────────────


def test_author_can_edit_their_own_status(client, make_hospital, make_staff, patient_for, auth):
    hospital = make_hospital("Status Edit Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@statusedit.test")
    patient = patient_for(hospital)
    entry = make_status(patient, nurse, "Waiting")

    response = client.patch(
        status_detail_url(entry.id),
        data=json.dumps({"name": "Stable"}),
        content_type="application/json",
        **auth(nurse.email),
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Stable"


def test_hospital_admin_can_edit_anyones_status(client, make_hospital, make_staff, patient_for, auth):
    hospital = make_hospital("Admin Edit Status Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@adminstatusedit.test")
    patient = patient_for(hospital)
    entry = make_status(patient, nurse, "Waiting")

    response = client.patch(
        status_detail_url(entry.id),
        data=json.dumps({"name": "Stable"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200


def test_a_different_staff_member_cannot_edit_someone_elses_status(
    client, make_hospital, make_staff, patient_for, auth
):
    hospital = make_hospital("Status Ownership Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@statusown.test")
    other_nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="other@statusown.test")
    patient = patient_for(hospital)
    entry = make_status(patient, nurse, "Not yours")

    response = client.patch(
        status_detail_url(entry.id),
        data=json.dumps({"name": "Trying to edit"}),
        content_type="application/json",
        **auth(other_nurse.email),
    )

    assert response.status_code == 403


def test_author_can_delete_their_own_status(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Status Delete Hospital")
    patient = patient_for(hospital)
    entry = make_status(patient, hospital.admin)

    response = client.delete(status_detail_url(entry.id), **auth(hospital.admin.email))

    assert response.status_code == 204
    assert not PatientStatus.objects.filter(id=entry.id).exists()


def test_another_hospitals_status_resolves_to_404(client, make_hospital, patient_for, auth):
    alpha = make_hospital("Alpha Status Detail Isolation")
    beta = make_hospital("Beta Status Detail Isolation")
    beta_patient = patient_for(beta)
    beta_status = make_status(beta_patient, beta.admin)

    response = client.get(status_detail_url(beta_status.id), **auth(alpha.admin.email))

    assert response.status_code == 404
