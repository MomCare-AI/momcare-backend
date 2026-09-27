"""generate_patient_summary() -- the prompt assembly + client call + upsert,
end to end, with the OpenRouter client mocked."""

from unittest.mock import patch

import pytest
from django.db import connection
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


def test_the_client_call_does_not_run_while_the_patient_row_is_locked(patient):
    """Critical review finding: the whole function used to run inside one
    transaction.atomic() holding SELECT ... FOR UPDATE on the patient row for
    the entire ~30s HTTP call. The lock must be acquired only around the
    final upsert, after the client call has already returned."""
    events: list[str] = []

    def query_wrapper(execute, sql, params, many, context):
        if "FOR UPDATE" in sql:
            events.append("select_for_update")
        return execute(sql, params, many, context)

    def fake_generate(prompt, *, model, max_tokens):
        events.append("client_call")
        return "ok"

    with connection.execute_wrapper(query_wrapper):
        with patch("momcare_platform.core.ai.openrouter_client.generate", side_effect=fake_generate):
            generate_patient_summary(patient)

    # update_or_create() itself takes out a second FOR UPDATE internally
    # (Django locks the row it's about to write) -- what this test actually
    # guards is that every lock comes strictly after the client call, never
    # before or during it.
    assert events[0] == "client_call"
    assert set(events[1:]) == {"select_for_update"}
    assert len(events) >= 2


def test_missing_fields_are_stated_as_not_on_file_not_silently_dropped(patient):
    """Important review finding: the base prompt instructs the model to
    'state that plainly instead of omitting it' for missing data, but the
    code building data_lines silently dropped any empty/None field instead
    of saying so -- contradicting its own instruction to the model. A freshly
    onboarded patient with no pregnancy yet is exactly this case."""
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        generate_patient_summary(patient)

    sent_prompt = mock_generate.call_args.args[0]
    assert "- gestational_age: not on file" in sent_prompt
    assert "- recent_note: not on file" in sent_prompt


def test_a_snapshot_building_error_is_logged_and_never_raised(patient):
    """Important review finding: only the client call was best-effort --
    an exception building the data snapshot (a DB error, a bad relation)
    used to propagate straight into the caller (a signal handler mid-request,
    or a cron sweep iterating many patients)."""
    with (
        patch(
            "momcare_platform.core.ai.services._build_data_snapshot",
            side_effect=RuntimeError("boom"),
        ),
        patch("momcare_platform.core.ai.openrouter_client.generate") as mock_generate,
    ):
        generate_patient_summary(patient)  # must not raise

    assert not mock_generate.called
    assert not AISummary.objects.filter(patient=patient).exists()
