"""generate_patient_summary() -- the prompt assembly + client call + upsert,
end to end, with the OpenRouter client mocked."""

from unittest.mock import patch

import pytest
from django.utils import timezone

from momcare_platform.core.ai.models import AISummary
from momcare_platform.core.ai.services import generate_patient_summary
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db


@pytest.fixture
def patient(make_hospital):
    hospital = make_hospital("Generate Summary Hospital")
    return onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Sana", "last_name": "Malik"},
    )


def test_generate_patient_summary_creates_the_ai_summary_row(patient):
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value="Sana has no readings yet this month.",
    ) as mock_generate:
        generate_patient_summary(patient)

    summary = AISummary.objects.get(patient=patient)
    assert summary.content == "Sana has no readings yet this month."
    assert mock_generate.called


def test_generate_patient_summary_is_a_no_op_when_the_client_fails(patient):
    AISummary.objects.create(
        patient=patient,
        content="Yesterday's summary.",
        generated_at=timezone.now(),
        model_used="google/gemini-2.0-flash-001",
    )

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        generate_patient_summary(patient)

    summary = AISummary.objects.get(patient=patient)
    assert summary.content == "Yesterday's summary."


def test_calling_it_twice_leaves_exactly_one_row(patient):
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="First."):
        generate_patient_summary(patient)
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Second."):
        generate_patient_summary(patient)

    assert AISummary.objects.filter(patient=patient).count() == 1
    assert AISummary.objects.get(patient=patient).content == "Second."


def test_the_word_cap_and_organization_instructions_reach_the_prompt(patient):
    patient.organization.ai_custom_instructions = "Always mention medication adherence."
    patient.organization.save(update_fields=["ai_custom_instructions", "updated_at"])

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        generate_patient_summary(patient)

    sent_prompt = mock_generate.call_args.args[0]
    assert "150" in sent_prompt
    assert "Always mention medication adherence." in sent_prompt


def test_deactivation_flag_changes_the_closing_instruction(patient):
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        generate_patient_summary(patient, deactivated=True)

    sent_prompt = mock_generate.call_args.args[0]
    assert "deactivated" in sent_prompt.lower()
    assert "recommendation" not in sent_prompt.lower() or "instead of a forward-looking recommendation" in sent_prompt
