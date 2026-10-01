"""_resolve_active_template() and _build_prompt()'s template-aware branch --
the platform's one active template wins over the built-in default two-section
layout. Hospitals have no templates of their own (removed 2026-10-01); a
leftover hospital-level row is ignored."""

from unittest.mock import patch

import pytest

from momcare_platform.core.ai.models import AISummaryTemplate
from momcare_platform.core.ai.services import (
    TEMPLATE_FIELD_VOCABULARY,
    _resolve_active_template,
    activate_summary_template,
    generate_patient_summary,
)
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db


def _full_content():
    return "Readings, then risk, then everything else about the patient."


def test_resolve_returns_the_seeded_default_when_nothing_else_is_active():
    assert _resolve_active_template().name == "Default Summary Template"


def test_resolve_returns_none_when_every_template_is_deactivated():
    AISummaryTemplate.objects.update(is_active=False)

    assert _resolve_active_template() is None


def test_resolve_returns_the_active_platform_template():
    template = AISummaryTemplate.objects.create(organization=None, name="Platform", content=_full_content())
    activate_summary_template(template)

    assert _resolve_active_template() == template


def test_resolve_ignores_an_active_hospital_level_template(make_hospital):
    """A hospital-level row can no longer be created through the API, but if
    one exists (e.g. a restore from an old backup) it must never shape any
    summary."""
    hospital = make_hospital("Template Resolve Ignore Org Hospital")
    AISummaryTemplate.objects.create(
        organization=hospital.org,
        name="Stray",
        content=_full_content(),
        is_active=True,
    )

    assert _resolve_active_template().name == "Default Summary Template"


def test_an_active_template_guides_the_real_generated_prompt(make_hospital):
    """End-to-end: the admin's own plain wording reaches the prompt as
    guidance, followed by the real patient's data for all 17 fields."""
    hospital = make_hospital("Template Prompt Reshape Hospital")
    patient = onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Amina", "last_name": "Yousaf"},
    )
    template = AISummaryTemplate.objects.create(
        organization=None,
        name="Name Last",
        content="Start with the latest reading like 120/80, then the risk level, and the patient name last.",
    )
    activate_summary_template(template)

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        generate_patient_summary(patient)

    sent_prompt = mock_generate.call_args.args[0]
    assert "Start with the latest reading like 120/80" in sent_prompt
    assert "patient_name: Amina Yousaf" in sent_prompt
    for field in TEMPLATE_FIELD_VOCABULARY:
        assert f"- {field}:" in sent_prompt
    assert "Vitals & Risk:" not in sent_prompt
    assert sent_prompt.index("Start with the latest reading") < sent_prompt.index("patient_name: Amina Yousaf")
