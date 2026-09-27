"""The two signal-based triggers: patient creation and a risk-level change.
No manual regenerate endpoint exists -- these, plus the periodic command
(Task 9) and the deactivation call (Task 10), are the only paths in."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.apps import apps as django_apps
from django.utils import timezone

from momcare_platform.core.ai.models import AISummary
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db

# Resolved via the app registry, not a static import: both live in
# modules.pregnancy.vitals, which core (this test included) must never
# import statically -- the `core must not import modules` contract.
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
LOW_VITALS = {
    "systolic_bp": 110,
    "diastolic_bp": 70,
    "heart_rate": 75,
    "body_temp_f": 98.6,
    "hemoglobin": 12.0,
    "blood_glucose": 90,
    "stress_score": 2,
    "phys_activity_score": 7,
}


def test_creating_a_patient_generates_the_first_summary(make_hospital):
    hospital = make_hospital("Enrollment Trigger Hospital")

    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value="Newly enrolled, no data yet.",
    ) as mock_generate:
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Hina", "last_name": "Yousaf"},
        )

    assert mock_generate.called
    assert AISummary.objects.get(patient=patient).content == "Newly enrolled, no data yet."


def test_enrollment_summary_includes_pregnancy_data_when_provided_at_onboarding(make_hospital):
    """Important review finding: the enrollment trigger used to fire from a
    Patient post_save signal, which runs before onboard_patient() creates
    the pregnancy -- so a patient onboarded WITH obstetric data always got a
    first summary describing her as having no pregnancy at all."""
    hospital = make_hospital("Enrollment With Pregnancy Hospital")

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Farah", "last_name": "Baig"},
            pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=28)},
        )

    assert mock_generate.called
    sent_prompt = mock_generate.call_args.args[0]
    assert "gestational_age" in sent_prompt
    assert "not on file" not in sent_prompt.split("gestational_age")[1].split("\n")[0]


def test_a_risk_level_change_triggers_a_regeneration(make_hospital):
    hospital = make_hospital("Risk Change Trigger Hospital")
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Enrollment summary."):
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Rabia", "last_name": "Anwar"},
            pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=28)},
        )
    pregnancy = patient.current_pregnancy
    AISummary.objects.filter(patient=patient).update(risk_level_at_generation=RiskAssessment.LEVEL_LOW)

    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value="Risk just went High.",
    ) as mock_generate:
        VitalReading.objects.create(
            pregnancy=pregnancy,
            recorded_at=timezone.now(),
            source=VitalReading.SOURCE_MANUAL,
            **HIGH_VITALS,
        )

    assert mock_generate.called
    assert AISummary.objects.get(patient=patient).content == "Risk just went High."


def test_a_reading_that_does_not_change_the_risk_level_does_not_trigger_a_regeneration(make_hospital):
    hospital = make_hospital("Risk Unchanged Hospital")
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Enrollment summary."):
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Nida", "last_name": "Chaudhry"},
            pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=20)},
        )
    pregnancy = patient.current_pregnancy
    AISummary.objects.filter(patient=patient).update(risk_level_at_generation=RiskAssessment.LEVEL_LOW)

    with patch("momcare_platform.core.ai.openrouter_client.generate") as mock_generate:
        VitalReading.objects.create(
            pregnancy=pregnancy,
            recorded_at=timezone.now(),
            source=VitalReading.SOURCE_MANUAL,
            **LOW_VITALS,
        )

    assert not mock_generate.called
    assert AISummary.objects.get(patient=patient).content == "Enrollment summary."
