"""``/patients/<id>/monitoring/`` -- the combined create/list endpoint."""

import json
from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db


@pytest.fixture
def patient_for(make_hospital):
    def _make(hospital, first_name="Ayesha", *, with_pregnancy=True):
        return onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
            pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=20)} if with_pregnancy else None,
        )

    return _make


def monitoring_url(patient_id):
    return f"/api/patients/{patient_id}/monitoring/"


def post_json(client, url, data, **headers):
    return client.post(url, data=json.dumps(data), content_type="application/json", **headers)


# ── Creating ──────────────────────────────────────────────────────────────


def test_hospital_admin_can_log_a_duration_only_contact(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Duration Log Hospital")
    patient = patient_for(hospital)

    response = post_json(
        client,
        monitoring_url(patient.id),
        {"duration_seconds": 600},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["session"]["duration_seconds"] == 600
    assert body["note"] is None


def test_a_note_only_contact_creates_no_session(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Note Log Hospital")
    patient = patient_for(hospital)

    response = post_json(
        client,
        monitoring_url(patient.id),
        {"note": "Called to check on nausea, advised hydration."},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["session"] is None
    assert body["note"]["note"] == "Called to check on nausea, advised hydration."


def test_neither_duration_nor_note_is_rejected(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Empty Payload Hospital")
    patient = patient_for(hospital)

    response = post_json(client, monitoring_url(patient.id), {}, **auth(hospital.admin.email))

    assert response.status_code == 400


def test_note_carries_the_current_pregnancy(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Pregnancy Carry Hospital")
    patient = patient_for(hospital)
    expected = str(patient.current_pregnancy.id)

    response = post_json(
        client,
        monitoring_url(patient.id),
        {"note": "Reviewed her chart."},
        **auth(hospital.admin.email),
    )

    assert response.json()["note"]["pregnancy_id"] == expected


def test_tags_can_be_created_inline_by_name(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Inline Tag Hospital")
    patient = patient_for(hospital)

    response = post_json(
        client,
        monitoring_url(patient.id),
        {"note": "Needs follow-up next week.", "tags": [{"name": "Follow Up", "color": "#00ff00"}]},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    tags = response.json()["note"]["tags"]
    assert [t["name"] for t in tags] == ["Follow Up"]


def test_call_outcome_flags_cannot_both_be_true(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Both Outcomes API Hospital")
    patient = patient_for(hospital)

    response = post_json(
        client,
        monitoring_url(patient.id),
        {"note": "Called.", "left_voicemail": True, "two_way_communication": True},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400


def test_a_patient_role_cannot_log_a_contact(client, make_hospital, patient_for, auth):
    from momcare_platform.core.users.models import Role, User  # noqa: PLC0415

    hospital = make_hospital("Patient Block Log Hospital")
    patient = patient_for(hospital)
    mother = User.objects.create_user(
        email="mother@blocklog.test",
        password="MotherPass!2026",
        first_name="Mother",
        last_name="Block",
        role=Role.objects.get(code=settings.ROLE_PATIENT),
    )
    mother.organization = hospital.org
    mother.is_email_verified = True
    mother.save(update_fields=["organization", "is_email_verified", "updated_at"])

    response = post_json(
        client,
        monitoring_url(patient.id),
        {"note": "Should not be allowed."},
        **auth(mother.email, "MotherPass!2026"),
    )

    assert response.status_code == 403


# ── Cross-tenant isolation ───────────────────────────────────────────────


def test_another_hospitals_patient_resolves_to_404(client, make_hospital, patient_for, auth):
    alpha = make_hospital("Alpha Monitoring")
    beta = make_hospital("Beta Monitoring")
    beta_patient = patient_for(beta)

    response = post_json(
        client,
        monitoring_url(beta_patient.id),
        {"note": "Should not reach beta's patient."},
        **auth(alpha.admin.email),
    )

    assert response.status_code == 404


# ── Listing (merged timeline) ─────────────────────────────────────────────


def test_the_combined_list_includes_standalone_notes_and_sessions(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Merged Timeline Hospital")
    patient = patient_for(hospital)
    post_json(client, monitoring_url(patient.id), {"duration_seconds": 300}, **auth(hospital.admin.email))
    post_json(client, monitoring_url(patient.id), {"note": "Standalone note."}, **auth(hospital.admin.email))
    post_json(
        client,
        monitoring_url(patient.id),
        {"duration_seconds": 120, "note": "Paired with a session."},
        **auth(hospital.admin.email),
    )

    response = client.get(monitoring_url(patient.id), **auth(hospital.admin.email))

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3
    entries = body["results"]
    paired = next(e for e in entries if e["note"] and e["note"]["note"] == "Paired with a session.")
    assert paired["session"] is not None
    standalone = next(e for e in entries if e["note"] and e["note"]["note"] == "Standalone note.")
    assert standalone["session"] is None


def test_the_combined_list_never_shows_another_hospitals_entries(client, make_hospital, patient_for, auth):
    alpha = make_hospital("Alpha List Hospital")
    beta = make_hospital("Beta List Hospital")
    alpha_patient = patient_for(alpha)
    beta_patient = patient_for(beta)
    post_json(client, monitoring_url(alpha_patient.id), {"note": "Alpha's note."}, **auth(alpha.admin.email))
    post_json(client, monitoring_url(beta_patient.id), {"note": "Beta's note."}, **auth(beta.admin.email))

    response = client.get(monitoring_url(alpha_patient.id), **auth(alpha.admin.email))

    assert response.json()["count"] == 1


# ── Monthly totals ────────────────────────────────────────────────────────


def test_the_combined_feed_reports_the_current_month_total_by_default(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Combined Totals Hospital")
    patient = patient_for(hospital)
    post_json(client, monitoring_url(patient.id), {"duration_seconds": 300}, **auth(hospital.admin.email))
    post_json(client, monitoring_url(patient.id), {"duration_seconds": 600}, **auth(hospital.admin.email))
    post_json(client, monitoring_url(patient.id), {"note": "No duration on this one."}, **auth(hospital.admin.email))

    response = client.get(monitoring_url(patient.id), **auth(hospital.admin.email))

    body = response.json()
    now = timezone.now()
    assert body["year"] == now.year
    assert body["month"] == now.month
    assert body["totals"]["total_seconds"] == 900
    assert body["count"] == 3


def test_the_combined_feed_is_scoped_to_the_requested_month(client, make_hospital, patient_for, auth):
    from datetime import datetime  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    from momcare_platform.core.monitoring.models import MonitoringNote, MonitoringSession  # noqa: PLC0415

    hospital = make_hospital("Combined Month Scope Hospital")
    patient = patient_for(hospital)
    last_year = datetime(2020, 6, 10, 12, 0, tzinfo=ZoneInfo("UTC"))
    MonitoringSession.objects.create(
        patient=patient,
        pregnancy=patient.current_pregnancy,
        duration_seconds=400,
        added_by=hospital.admin,
        recorded_at=last_year,
    )
    MonitoringNote.objects.create(
        patient=patient,
        pregnancy=patient.current_pregnancy,
        note="An old standalone note.",
        added_by=hospital.admin,
        recorded_at=last_year,
    )

    current_month_response = client.get(monitoring_url(patient.id), **auth(hospital.admin.email))
    assert current_month_response.json()["count"] == 0
    assert current_month_response.json()["totals"]["total_seconds"] == 0

    old_month_response = client.get(
        f"{monitoring_url(patient.id)}?year=2020&month=6",
        **auth(hospital.admin.email),
    )
    body = old_month_response.json()
    assert body["count"] == 2
    assert body["totals"]["total_seconds"] == 400


def test_the_combined_feed_rejects_an_invalid_month(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Combined Bad Month Hospital")
    patient = patient_for(hospital)

    response = client.get(f"{monitoring_url(patient.id)}?month=0", **auth(hospital.admin.email))

    assert response.status_code == 400
