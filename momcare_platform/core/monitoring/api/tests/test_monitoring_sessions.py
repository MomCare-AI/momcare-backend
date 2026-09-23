"""``/patients/<id>/monitoring/sessions/`` and ``/monitoring-sessions/<id>/``."""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.utils import timezone

from momcare_platform.core.monitoring.models import MonitoringSession
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


def sessions_url(patient_id):
    return f"/api/patients/{patient_id}/monitoring/sessions/"


def session_detail_url(session_id):
    return f"/api/monitoring-sessions/{session_id}/"


def make_session(patient, added_by, duration=300):
    return MonitoringSession.objects.create(
        patient=patient,
        pregnancy=patient.current_pregnancy,
        duration_seconds=duration,
        added_by=added_by,
    )


def test_sessions_list_reports_the_current_month_total_by_default(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Session Totals Hospital")
    patient = patient_for(hospital)
    make_session(patient, hospital.admin, duration=300)
    make_session(patient, hospital.admin, duration=600)

    response = client.get(sessions_url(patient.id), **auth(hospital.admin.email))

    body = response.json()
    now = timezone.now()
    assert body["year"] == now.year
    assert body["month"] == now.month
    assert body["totals"]["total_seconds"] == 900


def test_sessions_list_totals_exclude_a_different_month(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Session Totals Other Month Hospital")
    patient = patient_for(hospital)
    make_session(patient, hospital.admin, duration=300)
    other = MonitoringSession.objects.create(
        patient=patient,
        pregnancy=patient.current_pregnancy,
        duration_seconds=700,
        added_by=hospital.admin,
        recorded_at=timezone.now() - timedelta(days=90),
    )

    response = client.get(sessions_url(patient.id), **auth(hospital.admin.email))

    body = response.json()
    assert body["totals"]["total_seconds"] == 300
    assert all(row["id"] != str(other.id) for row in body["results"])


def test_sessions_list_year_and_month_params_select_a_different_period(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Session Totals Explicit Month Hospital")
    patient = patient_for(hospital)
    MonitoringSession.objects.create(
        patient=patient,
        pregnancy=patient.current_pregnancy,
        duration_seconds=500,
        added_by=hospital.admin,
        recorded_at=datetime(2026, 3, 10, 12, 0, tzinfo=ZoneInfo("UTC")),
    )

    response = client.get(f"{sessions_url(patient.id)}?year=2026&month=3", **auth(hospital.admin.email))

    body = response.json()
    assert body["year"] == 2026
    assert body["month"] == 3
    assert body["totals"]["total_seconds"] == 500
    assert body["count"] == 1


def test_sessions_list_rejects_a_month_out_of_range(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Session Totals Bad Month Hospital")
    patient = patient_for(hospital)

    response = client.get(f"{sessions_url(patient.id)}?month=13", **auth(hospital.admin.email))

    assert response.status_code == 400


def test_sessions_list_rejects_a_non_integer_year(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Session Totals Bad Year Hospital")
    patient = patient_for(hospital)

    response = client.get(f"{sessions_url(patient.id)}?year=not-a-year", **auth(hospital.admin.email))

    assert response.status_code == 400


def test_sessions_list_is_scoped_to_the_patient(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Session List Hospital")
    patient = patient_for(hospital)
    other_patient = patient_for(hospital, "Sana")
    make_session(patient, hospital.admin)
    make_session(other_patient, hospital.admin)

    response = client.get(sessions_url(patient.id), **auth(hospital.admin.email))

    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_author_can_edit_their_own_session(client, make_hospital, make_staff, patient_for, auth):
    hospital = make_hospital("Session Edit Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@sessionedit.test")
    patient = patient_for(hospital)
    session = make_session(patient, nurse)

    response = client.patch(
        session_detail_url(session.id),
        data=json.dumps({"duration_seconds": 900}),
        content_type="application/json",
        **auth(nurse.email),
    )

    assert response.status_code == 200
    assert response.json()["duration_seconds"] == 900


def test_a_different_staff_member_cannot_edit_someone_elses_session(
    client, make_hospital, make_staff, patient_for, auth
):
    hospital = make_hospital("Session Ownership Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@sessionown.test")
    other_nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="other@sessionown.test")
    patient = patient_for(hospital)
    session = make_session(patient, nurse)

    response = client.patch(
        session_detail_url(session.id),
        data=json.dumps({"duration_seconds": 900}),
        content_type="application/json",
        **auth(other_nurse.email),
    )

    assert response.status_code == 403


def test_hospital_admin_can_edit_anyones_session(client, make_hospital, make_staff, patient_for, auth):
    hospital = make_hospital("Session Admin Override Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@sessionadmin.test")
    patient = patient_for(hospital)
    session = make_session(patient, nurse)

    response = client.patch(
        session_detail_url(session.id),
        data=json.dumps({"duration_seconds": 900}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200


def test_author_can_delete_their_own_session(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Session Delete Hospital")
    patient = patient_for(hospital)
    session = make_session(patient, hospital.admin)

    response = client.delete(session_detail_url(session.id), **auth(hospital.admin.email))

    assert response.status_code == 204
    assert not MonitoringSession.objects.filter(id=session.id).exists()


def test_deleting_a_session_with_a_linked_note_cascades(client, make_hospital, patient_for, auth):
    from momcare_platform.core.monitoring.models import MonitoringNote  # noqa: PLC0415

    hospital = make_hospital("Session Cascade Hospital")
    patient = patient_for(hospital)
    session = make_session(patient, hospital.admin)
    note = MonitoringNote.objects.create(
        patient=patient,
        pregnancy=patient.current_pregnancy,
        session=session,
        note="Linked note.",
        added_by=hospital.admin,
    )

    client.delete(session_detail_url(session.id), **auth(hospital.admin.email))

    assert not MonitoringNote.objects.filter(id=note.id).exists()


def test_another_hospitals_session_resolves_to_404(client, make_hospital, patient_for, auth):
    alpha = make_hospital("Alpha Session Isolation")
    beta = make_hospital("Beta Session Isolation")
    beta_patient = patient_for(beta)
    beta_session = make_session(beta_patient, beta.admin)

    response = client.get(session_detail_url(beta_session.id), **auth(alpha.admin.email))

    assert response.status_code == 404


def test_duration_over_24_hours_is_rejected_on_update(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Session Bounds API Hospital")
    patient = patient_for(hospital)
    session = make_session(patient, hospital.admin)

    response = client.patch(
        session_detail_url(session.id),
        data=json.dumps({"duration_seconds": 90000}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400
