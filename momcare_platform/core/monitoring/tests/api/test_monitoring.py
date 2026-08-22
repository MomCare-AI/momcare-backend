"""Vital readings, device assignment, and the tenant boundary around both.

Readings are the most sensitive data in the system — a continuous record of a
woman's body — so the isolation tests here matter at least as much as the ones
guarding patient records.
"""

import json
from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from momcare_platform.core.monitoring.models import Device, VitalReading
from momcare_platform.core.monitoring.services import (
    MonitoringError,
    assign_device,
    latest_readings,
    simulate_readings,
)
from momcare_platform.core.patients.models import Consent, Pregnancy
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


@pytest.fixture
def device_for(db):
    def _make(hospital, serial="MC-0001"):
        return Device.objects.create(organization=hospital.org, serial_number=serial)

    return _make


def readings_url(pregnancy_id):
    return f"/api/pregnancies/{pregnancy_id}/readings/"


# ── Recording readings ───────────────────────────────────────────────────────


def test_staff_can_record_a_blood_pressure_by_hand(client, make_hospital, pregnancy_for, auth):
    """A nurse with a cuff must be able to use the system before any band exists."""
    hospital = make_hospital("Manual Hospital")
    pregnancy = pregnancy_for(hospital)

    response = client.post(
        readings_url(pregnancy.id),
        data=json.dumps(
            {"reading_type": "blood_pressure", "value": "128.0", "value_secondary": "82.0"},
        ),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["display_value"] == "128/82 mmHg"
    assert body["source"] == VitalReading.SOURCE_MANUAL
    assert body["is_simulated"] is False

    reading = VitalReading.objects.get(id=body["id"])
    assert reading.recorded_by == hospital.admin


def test_blood_pressure_needs_both_numbers(client, make_hospital, pregnancy_for, auth):
    """A systolic with no diastolic is not a blood pressure, and a rule
    evaluating 140/? could not decide anything."""
    hospital = make_hospital("Half BP Hospital")
    pregnancy = pregnancy_for(hospital)

    response = client.post(
        readings_url(pregnancy.id),
        data=json.dumps({"reading_type": "blood_pressure", "value": "140.0"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400
    assert "value_secondary" in response.json()


def test_systolic_must_exceed_diastolic(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Inverted BP Hospital")
    pregnancy = pregnancy_for(hospital)

    response = client.post(
        readings_url(pregnancy.id),
        data=json.dumps(
            {"reading_type": "blood_pressure", "value": "80.0", "value_secondary": "120.0"},
        ),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400


def test_a_single_valued_reading_rejects_a_second_number(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Extra Value Hospital")
    pregnancy = pregnancy_for(hospital)

    response = client.post(
        readings_url(pregnancy.id),
        data=json.dumps({"reading_type": "heart_rate", "value": "88", "value_secondary": "60"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400


def test_readings_are_refused_on_a_pregnancy_that_has_ended(
    client, make_hospital, pregnancy_for, auth,
):
    """A delivered pregnancy is closed history; new observations do not belong."""
    hospital = make_hospital("Ended Hospital")
    pregnancy = pregnancy_for(hospital)
    pregnancy.status = Pregnancy.STATUS_DELIVERED
    pregnancy.save(update_fields=["status", "updated_at"])

    response = client.post(
        readings_url(pregnancy.id),
        data=json.dumps({"reading_type": "heart_rate", "value": "88"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400


# ── Latest readings ──────────────────────────────────────────────────────────


def test_latest_returns_the_most_recent_of_each_type(make_hospital, pregnancy_for):
    hospital = make_hospital("Latest Hospital")
    pregnancy = pregnancy_for(hospital)
    now = timezone.now()

    VitalReading.objects.create(
        pregnancy=pregnancy, reading_type=VitalReading.TYPE_HEART_RATE,
        value=80, recorded_at=now - timedelta(hours=2),
    )
    newest = VitalReading.objects.create(
        pregnancy=pregnancy, reading_type=VitalReading.TYPE_HEART_RATE,
        value=95, recorded_at=now,
    )

    latest = latest_readings(pregnancy)
    assert latest[VitalReading.TYPE_HEART_RATE].id == newest.id


def test_a_type_with_no_readings_is_absent_not_normal(make_hospital, pregnancy_for):
    """Missing data must stay visibly missing. A screen that looks calm because
    readings stopped arriving is the worst failure this system could have."""
    hospital = make_hospital("Absent Hospital")
    pregnancy = pregnancy_for(hospital)
    VitalReading.objects.create(
        pregnancy=pregnancy, reading_type=VitalReading.TYPE_HEART_RATE,
        value=88, recorded_at=timezone.now(),
    )

    latest = latest_readings(pregnancy)

    assert VitalReading.TYPE_HEART_RATE in latest
    assert VitalReading.TYPE_BLOOD_PRESSURE not in latest
    assert VitalReading.TYPE_TEMPERATURE not in latest


# ── Devices ──────────────────────────────────────────────────────────────────


def test_assigning_a_device_links_readings_to_the_wearer(make_hospital, pregnancy_for, device_for):
    hospital = make_hospital("Wearer Hospital")
    pregnancy = pregnancy_for(hospital)
    device = device_for(hospital)

    assign_device(device=device, pregnancy=pregnancy, acquisition=Device.ACQUISITION_LOANED)

    device.refresh_from_db()
    assert device.status == Device.STATUS_ASSIGNED
    assert device.assigned_pregnancy == pregnancy
    assert device.is_assigned


def test_a_device_cannot_be_worn_by_two_patients(make_hospital, pregnancy_for, device_for):
    hospital = make_hospital("Two Wrists Hospital")
    first = pregnancy_for(hospital, first_name="First")
    second = pregnancy_for(hospital, first_name="Second")
    device = device_for(hospital)
    assign_device(device=device, pregnancy=first)

    with pytest.raises(MonitoringError, match="already assigned"):
        assign_device(device=device, pregnancy=second)


def test_a_patient_cannot_wear_two_devices(make_hospital, pregnancy_for, device_for):
    hospital = make_hospital("Two Bands Hospital")
    pregnancy = pregnancy_for(hospital)
    assign_device(device=device_for(hospital, "MC-A"), pregnancy=pregnancy)

    with pytest.raises(MonitoringError, match="already wearing"):
        assign_device(device=device_for(hospital, "MC-B"), pregnancy=pregnancy)


def test_a_device_from_another_hospital_is_refused(make_hospital, pregnancy_for, device_for):
    """Otherwise one hospital's device would file readings against another's patient."""
    alpha = make_hospital("Alpha Devices")
    beta = make_hospital("Beta Devices")
    pregnancy = pregnancy_for(alpha)
    beta_device = device_for(beta, "MC-BETA")

    with pytest.raises(MonitoringError, match="different hospital"):
        assign_device(device=beta_device, pregnancy=pregnancy)


def test_unassigning_keeps_the_readings_already_collected(
    client, make_hospital, pregnancy_for, device_for, auth,
):
    """Readings are observations of things that happened; returning the band
    does not unmake them."""
    hospital = make_hospital("Return Hospital")
    pregnancy = pregnancy_for(hospital)
    device = device_for(hospital)
    assign_device(device=device, pregnancy=pregnancy)
    simulate_readings(pregnancy=pregnancy, hours=2, device=device)
    before = pregnancy.readings.count()

    response = client.delete(
        f"/api/pregnancies/{pregnancy.id}/device/",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200
    assert pregnancy.readings.count() == before


# ── Simulation ───────────────────────────────────────────────────────────────


def test_simulated_readings_are_labelled_as_simulated(make_hospital, pregnancy_for):
    """The whole point: generated data must never be mistaken for measured data."""
    hospital = make_hospital("Simulated Hospital")
    pregnancy = pregnancy_for(hospital)

    simulate_readings(pregnancy=pregnancy, hours=6)

    assert pregnancy.readings.exists()
    assert pregnancy.readings.exclude(source=VitalReading.SOURCE_SIMULATED).count() == 0


def test_simulation_covers_every_reading_type(make_hospital, pregnancy_for):
    hospital = make_hospital("Coverage Hospital")
    pregnancy = pregnancy_for(hospital)

    simulate_readings(pregnancy=pregnancy, hours=24)

    types = set(pregnancy.readings.values_list("reading_type", flat=True))
    assert types == {
        VitalReading.TYPE_BLOOD_PRESSURE,
        VitalReading.TYPE_HEART_RATE,
        VitalReading.TYPE_TEMPERATURE,
    }


def test_elevated_simulation_produces_hypertensive_readings(make_hospital, pregnancy_for):
    """Needed to demonstrate risk detection without waiting for a patient to
    genuinely deteriorate."""
    hospital = make_hospital("Elevated Hospital")
    pregnancy = pregnancy_for(hospital)

    simulate_readings(pregnancy=pregnancy, hours=24, elevated=True)

    highest = (
        pregnancy.readings.filter(reading_type=VitalReading.TYPE_BLOOD_PRESSURE)
        .order_by("-value")
        .first()
    )
    assert highest.value >= 140, "elevated simulation should cross the hypertension threshold"


def test_simulation_is_refused_outside_development(client, make_hospital, pregnancy_for, auth, settings):
    """Simulated observations must not be creatable where real ones live."""
    settings.DEBUG = False
    hospital = make_hospital("Prod Sim Hospital")
    pregnancy = pregnancy_for(hospital)

    response = client.post(
        f"/api/pregnancies/{pregnancy.id}/readings/simulate/",
        data=json.dumps({"hours": 6}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 403
    assert pregnancy.readings.count() == 0


# ── Tenant isolation ─────────────────────────────────────────────────────────


def test_readings_are_not_visible_across_hospitals(
    client, make_hospital, pregnancy_for, auth,
):
    alpha = make_hospital("Alpha Readings")
    beta = make_hospital("Beta Readings")
    beta_pregnancy = pregnancy_for(beta)
    simulate_readings(pregnancy=beta_pregnancy, hours=4)

    response = client.get(readings_url(beta_pregnancy.id), **auth(alpha.admin.email))

    assert response.status_code == 404
    assert response.json()["detail"] == "Pregnancy not found."


def test_a_reading_cannot_be_filed_against_another_hospitals_patient(
    client, make_hospital, pregnancy_for, auth,
):
    alpha = make_hospital("Alpha Filing")
    beta = make_hospital("Beta Filing")
    beta_pregnancy = pregnancy_for(beta)

    response = client.post(
        readings_url(beta_pregnancy.id),
        data=json.dumps({"reading_type": "heart_rate", "value": "88"}),
        content_type="application/json",
        **auth(alpha.admin.email),
    )

    assert response.status_code == 404
    assert beta_pregnancy.readings.count() == 0


def test_device_list_never_crosses_hospitals(client, make_hospital, device_for, auth):
    alpha = make_hospital("Alpha Stock")
    beta = make_hospital("Beta Stock")
    device_for(alpha, "MC-ALPHA")
    device_for(beta, "MC-BETA")

    serials = {d["serial_number"] for d in client.get("/api/devices/", **auth(alpha.admin.email)).json()}

    assert serials == {"MC-ALPHA"}


def test_clinical_staff_can_record_readings(client, make_hospital, make_staff, pregnancy_for, auth):
    """Nurses take the measurements — recording one is not an admin task."""
    hospital = make_hospital("Nurse Reading Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@reading.test")
    pregnancy = pregnancy_for(hospital)

    response = client.post(
        readings_url(pregnancy.id),
        data=json.dumps({"reading_type": "temperature", "value": "37.1"}),
        content_type="application/json",
        **auth(nurse.email),
    )

    assert response.status_code == 201
