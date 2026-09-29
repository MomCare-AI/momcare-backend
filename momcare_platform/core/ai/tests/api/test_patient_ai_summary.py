"""GET /api/patients/{id}/ai-summary/ -- read-only, IsHospitalStaff-gated,
same tier as reading the rest of a patient's detail."""

from unittest.mock import patch

import pytest
from django.conf import settings

from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db


def summary_url(patient_id):
    return f"/api/patients/{patient_id}/ai-summary/"


@pytest.fixture
def patient(make_hospital):
    hospital = make_hospital("AI Summary Endpoint Hospital")
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Enrollment summary."):
        return onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Bushra", "last_name": "Tariq"},
        )


def test_hospital_staff_can_read_the_cached_summary(client, patient, auth):
    response = client.get(summary_url(patient.id), **auth(patient.organization.owner.email))

    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "Enrollment summary."
    assert body["citations"] == []


def test_returns_404_before_any_summary_exists(client, make_hospital, auth):
    hospital = make_hospital("AI Summary Missing Hospital")
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        patient_no_summary = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Amna", "last_name": "Riaz"},
        )

    response = client.get(summary_url(patient_no_summary.id), **auth(hospital.admin.email))

    assert response.status_code == 404


def test_a_patient_role_login_is_refused(client, patient, auth):
    from momcare_platform.core.users.models import Role, User

    User.objects.create_user(
        email="selfservice@aisummaryendpoint.test",
        password="TestPass!2026",
        first_name="Self",
        last_name="Service",
        role=Role.objects.get(code=settings.ROLE_PATIENT),
        is_email_verified=True,
    )

    response = client.get(summary_url(patient.id), **auth("selfservice@aisummaryendpoint.test"))

    assert response.status_code == 403


def test_another_hospitals_admin_gets_404_not_403(client, make_hospital, patient, auth):
    other_hospital = make_hospital("AI Summary Other Hospital")

    response = client.get(summary_url(patient.id), **auth(other_hospital.admin.email))

    assert response.status_code == 404


def test_a_provider_with_no_location_assignment_can_still_read_it(client, patient, auth, make_staff):
    """Important review finding: this view used LocationScopedQuerysetMixin
    while PatientDetailView (and every other patient-detail-adjacent view)
    uses organization scoping -- so a provider with no location assigned, or
    assigned to a different location than this patient's, could read the
    patient's own detail page but get a 404 on her AI summary."""
    provider = make_staff(patient.organization, settings.ROLE_PROVIDER, "noloc@aisummaryendpoint.test")

    response = client.get(summary_url(patient.id), **auth(provider.email))

    assert response.status_code == 200
    assert response.json()["content"] == "Enrollment summary."
