"""_resolve_active_template() and _build_prompt()'s template-aware branch --
an active org template wins over an active platform template, which wins
over the built-in default two-section layout. Precedence never merges, it
picks exactly one source, matching how instruction presets are NOT merged
across tiers for templates (unlike instructions, which do stack)."""

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


def _sections(*groups):
    """groups: list of (label, fields) tuples."""
    return [{"label": label, "fields": list(fields)} for label, fields in groups]


def _full_sections(label="All Fields"):
    return _sections((label, TEMPLATE_FIELD_VOCABULARY))


def test_resolve_returns_none_when_no_template_is_active_anywhere(make_hospital):
    hospital = make_hospital("Template Resolve Empty Hospital")

    assert _resolve_active_template(hospital.org) is None


def test_resolve_returns_the_organizations_own_active_template(make_hospital):
    hospital = make_hospital("Template Resolve Org Hospital")
    template = AISummaryTemplate.objects.create(
        organization=hospital.org,
        name="Org Template",
        sections=_full_sections(),
    )
    activate_summary_template(template)

    assert _resolve_active_template(hospital.org) == template


def test_resolve_falls_back_to_the_platform_template_when_the_org_has_none(make_hospital):
    hospital = make_hospital("Template Resolve Platform Fallback Hospital")
    platform_template = AISummaryTemplate.objects.create(
        organization=None,
        name="Platform Template",
        sections=_full_sections(),
    )
    activate_summary_template(platform_template)

    assert _resolve_active_template(hospital.org) == platform_template


def test_resolve_prefers_the_org_template_over_an_active_platform_template(make_hospital):
    hospital = make_hospital("Template Resolve Precedence Hospital")
    platform_template = AISummaryTemplate.objects.create(
        organization=None,
        name="Platform Template",
        sections=_full_sections(),
    )
    activate_summary_template(platform_template)
    org_template = AISummaryTemplate.objects.create(
        organization=hospital.org,
        name="Org Template",
        sections=_full_sections(),
    )
    activate_summary_template(org_template)

    assert _resolve_active_template(hospital.org) == org_template


def test_an_active_template_reshapes_the_real_generated_prompt(make_hospital):
    """End-to-end: an active org template's section labels reach the actual
    prompt sent to the client, replacing the built-in "Vitals & Risk:"/
    "Care Team & Activity:" layout entirely."""
    hospital = make_hospital("Template Prompt Reshape Hospital")
    patient = onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Amina", "last_name": "Yousaf"},
    )
    other_fields = [f for f in TEMPLATE_FIELD_VOCABULARY if f != "patient_name"]
    template = AISummaryTemplate.objects.create(
        organization=hospital.org,
        name="Name First",
        sections=_sections(("Who", ["patient_name"]), ("Everything Else", other_fields)),
    )
    activate_summary_template(template)

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        generate_patient_summary(patient)

    sent_prompt = mock_generate.call_args.args[0]
    assert "Who:" in sent_prompt
    assert "Everything Else:" in sent_prompt
    assert "Vitals & Risk:" not in sent_prompt
    assert "Care Team & Activity:" not in sent_prompt
    who_section = sent_prompt.split("Who:")[1].split("Everything Else:")[0]
    assert "patient_name: Amina Yousaf" in who_section
