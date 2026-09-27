"""_build_data_snapshot() -- the structured data handed to the LLM. Covers
both a data-rich patient and a brand-new one with almost nothing on file
(the Review Focus item: must not crash, must produce mostly-empty fields)."""

from datetime import timedelta

import pytest
from django.apps import apps as django_apps
from django.utils import timezone

from momcare_platform.core.ai.services import _build_data_snapshot
from momcare_platform.core.monitoring.models import MonitoringNote
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db

# Resolved via the app registry, not a static import: VitalReading lives in
# modules.pregnancy.vitals, which core (this test included) must never import
# statically -- the `core must not import modules` contract.
VitalReading = django_apps.get_model("monitoring", "VitalReading")

HIGH_VITALS = {
    "systolic_bp": 185,
    "diastolic_bp": 125,
    "heart_rate": 130,
    "body_temp_f": 103.0,
    "hemoglobin": 6.0,
    "blood_glucose": 250,
    "stress_score": 9,
    "phys_activity_score": 1,
}


@pytest.fixture
def patient_with_data(make_hospital, make_staff):
    from django.conf import settings

    hospital = make_hospital("Snapshot Data Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, email="provider@snapshotdata.test")
    patient = onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
        pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=28)},
    )
    pregnancy = patient.current_pregnancy
    pregnancy.provider = provider.staff
    pregnancy.save(update_fields=["provider", "updated_at"])

    VitalReading.objects.create(
        pregnancy=pregnancy,
        recorded_at=timezone.now(),
        source=VitalReading.SOURCE_MANUAL,
        **HIGH_VITALS,
    )
    MonitoringNote.objects.create(
        patient=patient,
        pregnancy=pregnancy,
        note="Routine check-in, patient reports feeling well.",
        added_by=hospital.admin,
    )
    return patient


@pytest.fixture
def brand_new_patient(make_hospital):
    hospital = make_hospital("Snapshot Empty Hospital")
    return onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Zara", "last_name": "Khan"},
    )


def test_snapshot_includes_current_risk_level_and_recent_note(patient_with_data):
    snapshot = _build_data_snapshot(patient_with_data)

    assert snapshot["current_risk_level"] == "high"
    assert snapshot["recent_note"] == "Routine check-in, patient reports feeling well."
    assert snapshot["provider_name"]


def test_snapshot_on_brand_new_patient_does_not_crash_and_is_mostly_empty(brand_new_patient):
    snapshot = _build_data_snapshot(brand_new_patient)

    assert snapshot["current_risk_level"] is None
    assert snapshot["gestational_age"] is None
    assert snapshot["recent_note"] is None
    assert snapshot["provider_name"] is None
    assert snapshot["active_statuses"] == []
