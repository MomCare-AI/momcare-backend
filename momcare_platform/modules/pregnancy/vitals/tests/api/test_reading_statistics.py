"""``GET /pregnancies/<id>/readings/`` with ``period``/``start_date``/
``end_date``/``reading_type``, and ``GET /pregnancies/<id>/vitals-summary/``.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from momcare_platform.core.patients.services import onboard_patient
from momcare_platform.modules.pregnancy.vitals.models import VitalReading

pytestmark = pytest.mark.django_db


@pytest.fixture
def pregnancy_for(db):
    def _make(hospital, *, first_name="Ayesha", weeks_pregnant=28):
        lmp = timezone.now().date() - timedelta(weeks=weeks_pregnant)
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
            pregnancy_data={"lmp": lmp},
        )
        return patient.current_pregnancy

    return _make


def readings_url(pregnancy_id):
    return f"/api/pregnancies/{pregnancy_id}/readings/"


def vitals_summary_url(pregnancy_id):
    return f"/api/pregnancies/{pregnancy_id}/vitals-summary/"


def _reading(pregnancy, *, recorded_at=None, **vitals):
    return VitalReading.objects.create(
        pregnancy=pregnancy,
        source=VitalReading.SOURCE_MANUAL,
        recorded_at=recorded_at or timezone.now(),
        **vitals,
    )


# ── reading_type triggers statistics, nothing else does ─────────────────


def test_no_reading_type_means_no_statistics_block(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("No Reading Type Hospital")
    pregnancy = pregnancy_for(hospital)
    _reading(pregnancy, systolic_bp="140", diastolic_bp="90")

    response = client.get(readings_url(pregnancy.id), **auth(hospital.admin.email))

    assert response.status_code == 200
    assert response.json()["statistics"] == {}


def test_reading_type_adds_the_statistics_block(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Reading Type Statistics Hospital")
    pregnancy = pregnancy_for(hospital)
    _reading(pregnancy, systolic_bp="140", diastolic_bp="90", heart_rate="80")

    response = client.get(f"{readings_url(pregnancy.id)}?reading_type=blood_pressure", **auth(hospital.admin.email))

    assert response.status_code == 200
    stats = response.json()["statistics"]
    assert stats["average"]["systolic_bp"] == 140.0
    assert "bp_category" in stats["categories"]


def test_statistics_are_scoped_to_only_the_requested_type(client, make_hospital, pregnancy_for, auth):
    """Requesting blood_pressure statistics must never include glucose, even
    though both were recorded on the same pregnancy."""
    hospital = make_hospital("Scoped Statistics Hospital")
    pregnancy = pregnancy_for(hospital)
    _reading(pregnancy, systolic_bp="140", diastolic_bp="90", heart_rate="80", blood_glucose="200")

    response = client.get(f"{readings_url(pregnancy.id)}?reading_type=blood_pressure", **auth(hospital.admin.email))

    stats = response.json()["statistics"]
    assert "blood_glucose" not in stats["average"]
    assert "glucose_category" not in stats["categories"]


def test_an_unknown_reading_type_is_rejected(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Unknown Reading Type API Hospital")
    pregnancy = pregnancy_for(hospital)

    response = client.get(f"{readings_url(pregnancy.id)}?reading_type=pulse_ox", **auth(hospital.admin.email))

    assert response.status_code == 400


# ── period / custom range ────────────────────────────────────────────────


def test_period_narrows_the_reading_list(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Period Filter Hospital")
    pregnancy = pregnancy_for(hospital)
    _reading(pregnancy, systolic_bp="120", diastolic_bp="80", recorded_at=timezone.now() - timedelta(days=100))
    _reading(pregnancy, systolic_bp="130", diastolic_bp="85")

    response = client.get(f"{readings_url(pregnancy.id)}?period=1_week", **auth(hospital.admin.email))

    assert response.json()["count"] == 1


def test_an_unknown_period_is_rejected(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Unknown Period API Hospital")
    pregnancy = pregnancy_for(hospital)

    response = client.get(f"{readings_url(pregnancy.id)}?period=2_weeks", **auth(hospital.admin.email))

    assert response.status_code == 400


def test_custom_range_takes_precedence_over_period(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Custom Range Precedence Hospital")
    pregnancy = pregnancy_for(hospital)
    old = timezone.now() - timedelta(days=100)
    _reading(pregnancy, systolic_bp="120", diastolic_bp="80", recorded_at=old)

    response = client.get(
        f"{readings_url(pregnancy.id)}?period=1_week&start_date={(old - timedelta(days=1)).date()}"
        f"&end_date={(old + timedelta(days=1)).date()}",
        **auth(hospital.admin.email),
    )

    assert response.json()["count"] == 1


def test_start_date_after_end_date_is_rejected(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Bad Range Hospital")
    pregnancy = pregnancy_for(hospital)

    response = client.get(
        f"{readings_url(pregnancy.id)}?start_date=2026-07-10&end_date=2026-07-01",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400


def test_no_date_filter_returns_full_history(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Full History Hospital")
    pregnancy = pregnancy_for(hospital)
    _reading(pregnancy, systolic_bp="120", diastolic_bp="80", recorded_at=timezone.now() - timedelta(days=400))
    _reading(pregnancy, systolic_bp="130", diastolic_bp="85")

    response = client.get(readings_url(pregnancy.id), **auth(hospital.admin.email))

    assert response.json()["count"] == 2


# ── Vitals Summary ────────────────────────────────────────────────────────


def test_vitals_summary_returns_last_30_days_average(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Vitals Summary API Hospital")
    pregnancy = pregnancy_for(hospital)
    _reading(pregnancy, systolic_bp="120", diastolic_bp="80", heart_rate="70")
    _reading(pregnancy, systolic_bp="130", diastolic_bp="90", heart_rate="80")

    response = client.get(vitals_summary_url(pregnancy.id), **auth(hospital.admin.email))

    assert response.status_code == 200
    averages = response.json()["last_30_days_average"]
    assert averages["systolic_bp"] == 125.0
    assert averages["blood_glucose"] is None


def test_vitals_summary_another_hospitals_pregnancy_resolves_to_404(client, make_hospital, pregnancy_for, auth):
    alpha = make_hospital("Alpha Vitals Summary Isolation")
    beta = make_hospital("Beta Vitals Summary Isolation")
    beta_pregnancy = pregnancy_for(beta)

    response = client.get(vitals_summary_url(beta_pregnancy.id), **auth(alpha.admin.email))

    assert response.status_code == 404
