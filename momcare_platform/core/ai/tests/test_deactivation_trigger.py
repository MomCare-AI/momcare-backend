"""Deactivating a patient writes one final AI Summary noting the transition,
then nothing else touches it while she stays inactive (see Task 9's own
skip-deactivated-patients test for that second half)."""

from unittest.mock import patch

import pytest

from momcare_platform.core.ai.models import AISummary
from momcare_platform.core.patients.services import deactivate_patient, onboard_patient

pytestmark = pytest.mark.django_db


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
