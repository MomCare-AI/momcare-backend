"""generate_patient_summary() -- the prompt assembly + client call + upsert,
end to end, with the OpenRouter client mocked."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.apps import apps as django_apps
from django.conf import settings
from django.db import connection
from django.utils import timezone

from momcare_platform.core.ai.models import AISummary, AISummaryTemplate
from momcare_platform.core.ai.services import (
    TEMPLATE_FIELD_VOCABULARY,
    activate_summary_template,
    deactivate_summary_template,
    generate_patient_summary,
)
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db

# Resolved via the app registry, not a static import -- VitalReading lives in
# modules.pregnancy.vitals, which core (this test included) must never import
# statically -- the `core must not import modules` contract.
VitalReading = django_apps.get_model("monitoring", "VitalReading")


def _full_sections(label="All Fields"):
    return [{"label": label, "fields": list(TEMPLATE_FIELD_VOCABULARY)}]


@pytest.fixture
def patient(make_hospital):
    hospital = make_hospital("Generate Summary Hospital")
    return onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Sana", "last_name": "Malik"},
    )


@pytest.fixture
def patient_with_provider_and_reading(make_hospital, make_staff):
    hospital = make_hospital("Generate Summary Citations Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, email="provider@citationsummary.test")
    patient = onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Amina", "last_name": "Yousaf"},
        pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=20)},
    )
    pregnancy = patient.current_pregnancy
    pregnancy.provider = provider.staff
    pregnancy.save(update_fields=["provider", "updated_at"])
    VitalReading.objects.create(
        pregnancy=pregnancy,
        recorded_at=timezone.now(),
        source=VitalReading.SOURCE_MANUAL,
        systolic_bp=185,
        diastolic_bp=125,
        heart_rate=130,
        body_temp_f=103.0,
        hemoglobin=6.0,
        blood_glucose=250,
    )
    return patient


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


def test_the_word_cap_and_active_organization_templates_extra_instructions_reach_the_prompt(patient):
    template = AISummaryTemplate.objects.create(
        organization=patient.organization,
        name="Adherence",
        sections=_full_sections(),
        extra_instructions="Always mention medication adherence.",
    )
    activate_summary_template(template)

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        generate_patient_summary(patient)

    sent_prompt = mock_generate.call_args.args[0]
    assert "150" in sent_prompt
    assert "Always mention medication adherence." in sent_prompt


def test_an_inactive_organization_templates_extra_instructions_never_reach_the_prompt(patient):
    AISummaryTemplate.objects.create(
        organization=patient.organization,
        name="Never activated",
        sections=_full_sections(),
        extra_instructions="Should never appear.",
    )

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        generate_patient_summary(patient)

    sent_prompt = mock_generate.call_args.args[0]
    assert "Should never appear." not in sent_prompt


def test_an_active_platform_templates_extra_instructions_reach_every_patients_prompt(patient):
    template = AISummaryTemplate.objects.create(
        organization=None,
        name="Platform-wide",
        sections=_full_sections(),
        extra_instructions="Always note the hospital's timezone.",
    )
    activate_summary_template(template)

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        generate_patient_summary(patient)

    sent_prompt = mock_generate.call_args.args[0]
    assert "Always note the hospital's timezone." in sent_prompt


def test_deactivating_the_only_active_platform_template_leaves_the_prompt_with_no_extra_instructions(patient):
    """Review Focus item: deactivating the platform's only active template,
    then generating a summary, must fall back to "no extra instructions"
    cleanly -- not crash on a missing template, and not keep sending stale
    content from the now-deactivated row."""
    template = AISummaryTemplate.objects.create(
        organization=None,
        name="Platform-wide",
        sections=_full_sections(),
        extra_instructions="Should disappear once deactivated.",
    )
    activate_summary_template(template)
    deactivate_summary_template(template)

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


def test_citations_are_computed_and_stored_for_values_the_ai_actually_mentioned(
    patient_with_provider_and_reading,
):
    """End to end: a reading value and a care-team name the AI's own text
    happens to mention get linked back to their real records. A value the
    text never mentions (a different, unmatched number) produces no
    citation -- proving the match is against the real generated text, not
    just "every known value always gets a citation"."""
    provider_name = patient_with_provider_and_reading.current_pregnancy.provider.user.get_full_name()
    # systolic_bp/diastolic_bp are DecimalField(decimal_places=2) -- the real
    # snapshot value is Decimal('185.00'), not the plain int 185, so the
    # fake AI text has to match that real formatted string for the citation
    # match to succeed, the same as it would have to in production.
    generated_text = f"{provider_name} recorded a reading of 185.00/125.00 during today's visit."

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=generated_text):
        generate_patient_summary(patient_with_provider_and_reading)

    summary = AISummary.objects.get(patient=patient_with_provider_and_reading)
    reading = VitalReading.objects.get(pregnancy=patient_with_provider_and_reading.current_pregnancy)
    assert {"text": "185.00/125.00", "type": "reading", "id": str(reading.id)} in summary.citations
    assert {
        "text": provider_name,
        "type": "staff",
        "id": str(patient_with_provider_and_reading.current_pregnancy.provider_id),
    } in summary.citations


def test_no_citation_for_a_value_the_generated_text_never_mentions(patient_with_provider_and_reading):
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value="This patient's vitals were reviewed and are stable overall.",
    ):
        generate_patient_summary(patient_with_provider_and_reading)

    summary = AISummary.objects.get(patient=patient_with_provider_and_reading)
    assert summary.citations == []


def test_a_blank_response_never_overwrites_the_previously_cached_summary(patient):
    """Real failure caught in live end-to-end testing: a reasoning-style
    model can spend its whole token budget on internal reasoning and
    return a non-None but blank/whitespace string. Before the fix, that
    blank string still passed the `content is None` check and overwrote a
    perfectly good previous summary with near-empty garbage. Every retry
    attempt returning blank must leave the existing row untouched, the
    same as a plain None already did."""
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Yesterday's real summary."):
        generate_patient_summary(patient)

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="   "):
        generate_patient_summary(patient)

    summary = AISummary.objects.get(patient=patient)
    assert summary.content == "Yesterday's real summary."


def test_a_blank_first_attempt_is_retried_and_a_later_success_is_stored(patient):
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=["", " ", "A real summary after retrying."],
    ) as mock_generate:
        generate_patient_summary(patient)

    summary = AISummary.objects.get(patient=patient)
    assert summary.content == "A real summary after retrying."
    assert mock_generate.call_count == 3
