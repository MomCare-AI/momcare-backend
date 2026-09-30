"""Deactivating a patient writes one final AI Summary noting the transition,
then nothing else touches it while she stays inactive (see Task 9's own
skip-deactivated-patients test for that second half)."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.apps import apps as django_apps
from django.utils import timezone

from momcare_platform.core.ai.models import AISummary
from momcare_platform.core.patients.services import deactivate_patient, onboard_patient

pytestmark = pytest.mark.django_db

# Resolved via the app registry, not a static import -- same reasoning as
# test_triggers.py: both live in modules.pregnancy.vitals, which core must
# never import statically.
RiskAssessment = django_apps.get_model("monitoring", "RiskAssessment")
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


def test_deactivating_a_patient_generates_a_final_summary(make_hospital):
    hospital = make_hospital("Deactivation Trigger Hospital")
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Enrollment summary."):
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Farah", "last_name": "Nawaz"},
        )

    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value="Active until today. Now deactivated.",
    ) as mock_generate:
        deactivate_patient(patient, by=hospital.admin)

    sent_prompt = mock_generate.call_args.args[0]
    assert "deactivated" in sent_prompt.lower()
    assert AISummary.objects.get(patient=patient).content == "Active until today. Now deactivated."


def test_a_risk_level_change_does_not_unfreeze_a_deactivated_patients_summary(make_hospital):
    """Important review finding: regenerate_for_new_reading() had no
    is_active check, so a new RiskAssessment on a deactivated patient's
    pregnancy (e.g. a late-arriving reading, an admin backfilling historical
    data) could silently overwrite the frozen deactivation summary with a
    forward-looking one -- exactly what the deactivation trigger's own
    closing-line fork exists to avoid."""
    hospital = make_hospital("Frozen Summary Hospital")
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Enrollment summary."):
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Kiran", "last_name": "Zafar"},
            pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=28)},
        )
    pregnancy = patient.current_pregnancy
    AISummary.objects.filter(patient=patient).update(risk_level_at_generation=RiskAssessment.LEVEL_LOW)

    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value="Active until today. Now deactivated.",
    ):
        deactivate_patient(patient, by=hospital.admin)

    with patch("momcare_platform.core.ai.openrouter_client.generate") as mock_generate:
        VitalReading.objects.create(
            pregnancy=pregnancy,
            recorded_at=timezone.now(),
            source=VitalReading.SOURCE_MANUAL,
            **HIGH_VITALS,
        )

    assert not mock_generate.called
    assert AISummary.objects.get(patient=patient).content == "Active until today. Now deactivated."
