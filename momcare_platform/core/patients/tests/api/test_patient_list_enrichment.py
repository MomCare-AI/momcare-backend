"""New Patient List fields added alongside the Care Activities work:
care-team names, language, gestational age's long display, and the
monitoring/reading signals already backing the Care Activity queues --
embedded here purely as a read-only display convenience, same pattern as
the pre-existing ``risk_level``/``gestational_age_display``.
"""

from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from momcare_platform.core.monitoring.models import MonitoringSession
from momcare_platform.core.patients.models import Patient
from momcare_platform.core.patients.services import onboard_patient
from momcare_platform.core.users.models import Role, User

pytestmark = pytest.mark.django_db

PATIENTS = "/api/patients/"


@pytest.fixture
def patient_for(make_hospital):
    def _make(hospital, first_name="Ayesha", *, pregnancy_data=None):
        return onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
            pregnancy_data=pregnancy_data,
        )

    return _make


def get_list(client, headers):
    return client.get(PATIENTS, **headers)


def test_gestational_age_long_display_is_present(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Long Display Hospital")
    patient_for(hospital, pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=30)})

    body = get_list(client, auth(hospital.admin.email)).json()

    row = body["results"][0]
    assert row["gestational_age_display"].endswith("d")
    assert "month" in row["gestational_age_long_display"]


def test_long_display_is_none_with_no_pregnancy(client, make_hospital, patient_for, auth):
    hospital = make_hospital("No Pregnancy Long Display Hospital")
    patient_for(hospital)

    body = get_list(client, auth(hospital.admin.email)).json()

    row = body["results"][0]
    assert row["gestational_age_display"] is None
    assert row["gestational_age_long_display"] is None


def test_care_team_names_are_embedded(client, make_hospital, make_staff, patient_for, auth):
    hospital = make_hospital("Care Team Embed Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@careteamembed.test")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, "provider@careteamembed.test")
    care_manager = make_staff(hospital.org, settings.ROLE_CARE_MANAGER, "cm@careteamembed.test")
    patient_for(
        hospital,
        pregnancy_data={
            "lmp": timezone.now().date(),
            "nurse": nurse.staff,
            "provider": provider.staff,
            "care_manager": care_manager.staff,
        },
    )

    body = get_list(client, auth(hospital.admin.email)).json()

    row = body["results"][0]
    assert row["nurse_name"] == nurse.get_full_name()
    assert row["provider_name"] == provider.get_full_name()
    assert row["care_manager_name"] == care_manager.get_full_name()


def test_care_team_names_are_none_with_no_pregnancy(client, make_hospital, patient_for, auth):
    hospital = make_hospital("No Pregnancy Care Team Hospital")
    patient_for(hospital)

    body = get_list(client, auth(hospital.admin.email)).json()

    row = body["results"][0]
    assert row["nurse_name"] is None
    assert row["provider_name"] is None
    assert row["care_manager_name"] is None


def test_language_reflects_the_patients_own_app_account(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Language Hospital")
    patient = patient_for(hospital)
    account = User.objects.create_user(
        email="patient@languagehospital.test",
        password="TestPass!2026",
        first_name="Ayesha",
        last_name="Bibi",
        role=Role.objects.get(code=settings.ROLE_PATIENT),
        language="ur",
    )
    Patient.objects.filter(pk=patient.pk).update(user=account)

    body = get_list(client, auth(hospital.admin.email)).json()

    assert body["results"][0]["language"] == "ur"


def test_language_is_none_without_an_app_account(client, make_hospital, patient_for, auth):
    hospital = make_hospital("No Account Language Hospital")
    patient_for(hospital)

    body = get_list(client, auth(hospital.admin.email)).json()

    assert body["results"][0]["language"] is None


def test_monitoring_seconds_this_month_reflects_logged_sessions(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Monitoring Seconds Embed Hospital")
    patient = patient_for(hospital)
    MonitoringSession.objects.create(
        patient=patient,
        duration_seconds=300,
        recorded_at=timezone.now(),
        added_by=hospital.admin,
    )

    body = get_list(client, auth(hospital.admin.email)).json()

    row = body["results"][0]
    assert row["monitoring_seconds_this_month"] == 300
    assert row["monitoring_time_display"] == "5m 0s"
    assert row["last_monitoring_contact_at"] is not None


def test_monitoring_fields_default_sensibly_with_no_activity(client, make_hospital, patient_for, auth):
    hospital = make_hospital("No Activity Hospital")
    patient_for(hospital)

    body = get_list(client, auth(hospital.admin.email)).json()

    row = body["results"][0]
    assert row["monitoring_seconds_this_month"] == 0
    assert row["monitoring_time_display"] == "0m 0s"
    assert row["last_monitoring_contact_at"] is None
    assert row["last_reading_at"] is None


# ── last_reading_display / last_monitoring_contact_display ─────────────────


def test_last_monitoring_contact_display_shows_today_for_a_session_just_now(
    client,
    make_hospital,
    patient_for,
    auth,
):
    hospital = make_hospital("Contact Display Today Hospital")
    patient = patient_for(hospital)
    MonitoringSession.objects.create(
        patient=patient,
        duration_seconds=300,
        recorded_at=timezone.now(),
        added_by=hospital.admin,
    )

    body = get_list(client, auth(hospital.admin.email)).json()

    assert body["results"][0]["last_monitoring_contact_display"] == "Today"


def test_last_monitoring_contact_display_shows_days_ago(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Contact Display Days Ago Hospital")
    patient = patient_for(hospital)
    MonitoringSession.objects.create(
        patient=patient,
        duration_seconds=300,
        recorded_at=timezone.now() - timedelta(days=6),
        added_by=hospital.admin,
    )

    body = get_list(client, auth(hospital.admin.email)).json()

    assert body["results"][0]["last_monitoring_contact_display"] == "6 days ago"


def test_last_reading_display_shows_days_ago(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Reading Display Hospital")
    patient = patient_for(hospital)
    Patient.objects.filter(pk=patient.pk).update(last_reading_at=timezone.now() - timedelta(days=1))

    body = get_list(client, auth(hospital.admin.email)).json()

    assert body["results"][0]["last_reading_display"] == "Yesterday"


def test_display_fields_are_none_with_no_activity(client, make_hospital, patient_for, auth):
    hospital = make_hospital("No Activity Display Hospital")
    patient_for(hospital)

    body = get_list(client, auth(hospital.admin.email)).json()

    row = body["results"][0]
    assert row["last_reading_display"] is None
    assert row["last_monitoring_contact_display"] is None


def _assess(pregnancy, level, *, minutes_ago=0, days_ago=0):
    from django.apps import apps as django_apps  # noqa: PLC0415

    RiskAssessment = django_apps.get_model("monitoring", "RiskAssessment")
    row = RiskAssessment.objects.create(
        pregnancy=pregnancy,
        risk_level=level,
        final_risk_level=level,
        confidence=0.95,
    )
    RiskAssessment.objects.filter(pk=row.pk).update(
        assessed_at=timezone.now() - timedelta(minutes=minutes_ago, days=days_ago),
    )


def _row(client, hospital, auth, name):
    rows = get_list(client, auth(hospital.admin.email)).json()["results"]
    return next(r for r in rows if r["full_name"].startswith(name))


def test_risk_this_month_is_not_assessed_with_no_readings(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Row Month None Hospital")
    patient_for(hospital, "Quiet", pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=20)})

    row = _row(client, hospital, auth, "Quiet")

    assert row["risk_this_month_level"] == "not_assessed"
    assert row["risk_latest_level"] == "not_assessed"
    assert row["risk_latest_level"] == "not_assessed"


def test_risk_this_month_summarises_the_month_while_risk_level_stays_the_latest(
    client,
    make_hospital,
    patient_for,
    auth,
):
    hospital = make_hospital("Row Month Mixed Hospital")
    patient = patient_for(hospital, "Mixed", pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=20)})
    for minutes_ago, level in [(30, "low"), (20, "low"), (10, "high")]:
        _assess(patient.current_pregnancy, level, minutes_ago=minutes_ago)

    row = _row(client, hospital, auth, "Mixed")

    assert row["risk_latest_level"] == "high"
    assert row["risk_this_month_level"] == "low"


def test_risk_this_month_ignores_last_months_assessments(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Row Month Old Hospital")
    patient = patient_for(hospital, "Old", pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=20)})
    _assess(patient.current_pregnancy, "high", days_ago=62)

    row = _row(client, hospital, auth, "Old")

    assert row["risk_latest_level"] == "high"
    assert row["risk_this_month_level"] == "not_assessed"


def test_risk_this_month_matches_vitals_summary(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Row Month Parity Hospital")
    patient = patient_for(hospital, "Parity", pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=20)})
    for minutes_ago, level in [(30, "medium"), (20, "high"), (10, "high")]:
        _assess(patient.current_pregnancy, level, minutes_ago=minutes_ago)

    row = _row(client, hospital, auth, "Parity")
    summary = client.get(
        f"/api/pregnancies/{patient.current_pregnancy.id}/vitals-summary/",
        **auth(hospital.admin.email),
    ).json()

    assert row["risk_this_month_level"] == summary["risk_this_month"]["most_common"]
