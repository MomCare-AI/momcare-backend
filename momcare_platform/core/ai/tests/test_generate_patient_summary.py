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


def test_the_word_cap_and_active_organization_preset_reach_the_prompt(patient):
    from momcare_platform.core.ai.models import AIInstructionPreset
    from momcare_platform.core.ai.services import activate_instruction_preset

    preset = AIInstructionPreset.objects.create(
        organization=patient.organization,
        name="Adherence",
        content="Always mention medication adherence.",
    )
    activate_instruction_preset(preset)

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        generate_patient_summary(patient)

    sent_prompt = mock_generate.call_args.args[0]
    assert "150" in sent_prompt
    assert "Always mention medication adherence." in sent_prompt


def test_an_inactive_organization_preset_never_reaches_the_prompt(patient):
    from momcare_platform.core.ai.models import AIInstructionPreset

    AIInstructionPreset.objects.create(
        organization=patient.organization,
        name="Never activated",
        content="Should never appear.",
    )

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        generate_patient_summary(patient)

    sent_prompt = mock_generate.call_args.args[0]
    assert "Should never appear." not in sent_prompt


def test_an_active_platform_preset_reaches_every_patients_prompt(patient):
    from momcare_platform.core.ai.models import AIInstructionPreset
    from momcare_platform.core.ai.services import activate_instruction_preset

    preset = AIInstructionPreset.objects.create(
        organization=None,
        name="Platform-wide",
        content="Always note the hospital's timezone.",
    )
    activate_instruction_preset(preset)

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        generate_patient_summary(patient)

    sent_prompt = mock_generate.call_args.args[0]
    assert "Always note the hospital's timezone." in sent_prompt


def test_deactivating_the_only_active_platform_preset_leaves_the_prompt_with_no_platform_section(patient):
    """Review Focus item: deactivating the platform's only active preset,
    then generating a summary, must fall back to "no platform instructions"
    cleanly -- not crash on a missing preset, and not keep sending stale
    content from the now-deactivated row."""
    from momcare_platform.core.ai.models import AIInstructionPreset
    from momcare_platform.core.ai.services import activate_instruction_preset, deactivate_instruction_preset

    preset = AIInstructionPreset.objects.create(
        organization=None,
        name="Platform-wide",
        content="Should disappear once deactivated.",
    )
    activate_instruction_preset(preset)
    deactivate_instruction_preset(preset)

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        generate_patient_summary(patient)

    sent_prompt = mock_generate.call_args.args[0]
    assert "Should disappear once deactivated." not in sent_prompt


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


def test_the_prompt_groups_data_by_topic_instead_of_one_flat_list(patient):
    """User-directed prompt redesign: the model was producing a field-by-field
    recitation ('X is not on file, Y is not on file...') instead of flowing
    clinical-note-style prose, because the prompt handed it one undifferentiated
    list of 17 facts in a row. Grouping related facts under labeled sections
    and explicitly asking for paragraphs-by-topic (matching how a clinician
    actually summarizes a chart) is meant to fix that."""
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        generate_patient_summary(patient)

    sent_prompt = mock_generate.call_args.args[0]
    assert "Vitals & Risk:" in sent_prompt
    assert "Care Team & Activity:" in sent_prompt
    # The vitals group's own fields land under that header, not the flat list.
    vitals_section = sent_prompt.split("Vitals & Risk:")[1].split("Care Team & Activity:")[0]
    assert "current_risk_level" in vitals_section
    care_team_section = sent_prompt.split("Care Team & Activity:")[1]
    assert "provider_name" in care_team_section
    assert "current_risk_level" not in care_team_section


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
