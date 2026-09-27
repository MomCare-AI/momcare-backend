"""refresh_ai_summaries -- the periodic safety net. Excludes deactivated
patients (Review Focus item); refreshes missing-or-stale summaries only."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone

from momcare_platform.core.ai.models import AISummary
from momcare_platform.core.patients.services import deactivate_patient, onboard_patient

pytestmark = pytest.mark.django_db


def test_refreshes_a_patient_with_no_summary_yet(make_hospital, settings):
    settings.MOMCARE_AI_SUMMARY_REFRESH_HOURS = 4
    hospital = make_hospital("Refresh Missing Hospital")
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Mahnoor", "last_name": "Ali"},
        )
    assert not AISummary.objects.filter(patient=patient).exists()

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Refreshed.") as mock_generate:
        call_command("refresh_ai_summaries")

    assert mock_generate.called
    assert AISummary.objects.get(patient=patient).content == "Refreshed."


def test_does_not_touch_a_summary_that_is_still_fresh(make_hospital, settings):
    settings.MOMCARE_AI_SUMMARY_REFRESH_HOURS = 4
    hospital = make_hospital("Refresh Fresh Hospital")
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Fresh content."):
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Iqra", "last_name": "Saeed"},
        )

    with patch("momcare_platform.core.ai.openrouter_client.generate") as mock_generate:
        call_command("refresh_ai_summaries")

    assert not mock_generate.called
    assert AISummary.objects.get(patient=patient).content == "Fresh content."


def test_skips_deactivated_patients(make_hospital, settings):
    settings.MOMCARE_AI_SUMMARY_REFRESH_HOURS = 4
    hospital = make_hospital("Refresh Deactivated Hospital")
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Original."):
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Warda", "last_name": "Iqbal"},
        )
    AISummary.objects.filter(patient=patient).update(
        generated_at=timezone.now() - timedelta(hours=10),
    )
    deactivate_patient(patient, by=hospital.admin)

    with patch("momcare_platform.core.ai.openrouter_client.generate") as mock_generate:
        call_command("refresh_ai_summaries")

    assert not mock_generate.called
    assert AISummary.objects.get(patient=patient).content == "Original."
