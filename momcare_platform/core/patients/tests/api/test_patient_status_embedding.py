"""``statuses`` embedded on Patient list/detail responses -- see
docs/design/2026-09-24-patient-statuses-design.md's Decision 3: full
history, newest first, on every patient row, matching Neuro_RPM's own
unconditional embed.
"""

import pytest
from django.utils import timezone

from momcare_platform.core.monitoring.models import PatientStatus
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db

PATIENTS = "/api/patients/"


@pytest.fixture
def patient(make_hospital):
    hospital = make_hospital("Status Embed Hospital")
    patient = onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
        pregnancy_data={"lmp": timezone.now().date()},
    )
    return hospital, patient


def test_patient_list_embeds_full_status_history(client, patient, auth):
    hospital, patient = patient
    PatientStatus.objects.create(
        patient=patient,
        name="Waiting",
        description="d1",
        color="#ffffff",
        added_by=hospital.admin,
    )
    PatientStatus.objects.create(
        patient=patient,
        name="Stable",
        description="d2",
        color="#00ff00",
        added_by=hospital.admin,
    )

    response = client.get(PATIENTS, **auth(hospital.admin.email))

    row = next(r for r in response.json()["results"] if r["id"] == str(patient.id))
    names = [s["name"] for s in row["statuses"]]
    assert names == ["Stable", "Waiting"]
    assert row["statuses"][0] == {"name": "Stable", "description": "d2", "color": "#00ff00"}


def test_patient_detail_embeds_full_status_history(client, patient, auth):
    hospital, patient = patient
    PatientStatus.objects.create(
        patient=patient,
        name="Critical",
        description="urgent",
        color="#ff0000",
        added_by=hospital.admin,
    )

    response = client.get(f"{PATIENTS}{patient.id}/", **auth(hospital.admin.email))

    assert response.json()["statuses"] == [{"name": "Critical", "description": "urgent", "color": "#ff0000"}]


def test_patient_with_no_statuses_has_an_empty_list(client, patient, auth):
    hospital, patient = patient

    response = client.get(f"{PATIENTS}{patient.id}/", **auth(hospital.admin.email))

    assert response.json()["statuses"] == []
