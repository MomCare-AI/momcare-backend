"""activate_summary_template()/deactivate_summary_template() -- same
transactional "at most one active per scope" swap as instruction presets,
plus the plain-text rules: not blank, within the word limit, and the AI-judged
check that every one of the 17 fields is covered."""

import json
from unittest.mock import patch

import pytest

from momcare_platform.core.ai.models import AISummaryTemplate
from momcare_platform.core.ai.services import (
    TEMPLATE_FIELD_VOCABULARY,
    activate_summary_template,
    check_template_coverage,
    deactivate_summary_template,
    validate_template_content,
)
from momcare_platform.core.ai.services import ActivationStateError as TemplateStateError

pytestmark = pytest.mark.django_db


def _full_content():
    """Every field once, in vocabulary order -- valid by construction."""
    return " ".join("{" + f + "}" for f in TEMPLATE_FIELD_VOCABULARY)


def test_activating_a_template_deactivates_the_previous_one_in_the_same_scope():
    old = AISummaryTemplate.objects.create(
        organization=None,
        name="Old",
        content=_full_content(),
        is_active=True,
    )
    new = AISummaryTemplate.objects.create(organization=None, name="New", content=_full_content())

    activate_summary_template(new)

    old.refresh_from_db()
    new.refresh_from_db()
    assert old.is_active is False
    assert new.is_active is True
    assert new.activated_at is not None


def test_activating_an_already_active_template_raises():
    template = AISummaryTemplate.objects.create(
        organization=None,
        name="Active",
        content=_full_content(),
        is_active=True,
    )

    with pytest.raises(TemplateStateError):
        activate_summary_template(template)


def test_the_active_template_cannot_be_deactivated():
    template = AISummaryTemplate.objects.create(
        organization=None,
        name="Active",
        content=_full_content(),
        is_active=True,
    )

    with pytest.raises(TemplateStateError, match="can't be deactivated"):
        deactivate_summary_template(template)

    template.refresh_from_db()
    assert template.is_active is True


def test_deactivating_an_already_inactive_template_raises():
    template = AISummaryTemplate.objects.create(
        organization=None,
        name="Inactive",
        content=_full_content(),
    )

    with pytest.raises(TemplateStateError):
        deactivate_summary_template(template)


def _ai_says(covered):
    return json.dumps({"covered": list(covered)})


def test_blank_content_is_rejected():
    assert validate_template_content("   ")


def test_plain_text_within_the_limit_passes_the_deterministic_rules():
    assert validate_template_content("reading 120/80, then risk, then the patient name") == []


def test_content_over_the_word_limit_is_rejected():
    errors = validate_template_content(" ".join(f"word{i}" for i in range(131)))

    assert any("limit is 130" in e for e in errors)


def test_content_at_exactly_the_word_limit_is_accepted():
    assert validate_template_content(" ".join(f"w{i}" for i in range(130))) == []


def test_coverage_reports_nothing_missing_when_the_ai_covers_all_17():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value=_ai_says(TEMPLATE_FIELD_VOCABULARY),
    ):
        assert check_template_coverage("anything") == []


def test_coverage_reports_exactly_the_fields_the_ai_did_not_cover():
    covered = [f for f in TEMPLATE_FIELD_VOCABULARY if f not in ("nurse_name", "recent_note")]
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=_ai_says(covered)):
        assert check_template_coverage("anything") == ["nurse_name", "recent_note"]


def test_coverage_accepts_json_wrapped_in_a_code_fence():
    fenced = "```json\n" + _ai_says(TEMPLATE_FIELD_VOCABULARY) + "\n```"
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=fenced):
        assert check_template_coverage("anything") == []


def test_coverage_ignores_keys_the_ai_invents():
    answer = _ai_says([*TEMPLATE_FIELD_VOCABULARY, "made_up"])
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=answer):
        assert check_template_coverage("anything") == []


def test_coverage_retries_once_when_the_first_answer_is_not_json():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=["I think it covers most of them", _ai_says(TEMPLATE_FIELD_VOCABULARY)],
    ):
        assert check_template_coverage("anything") == []


def test_coverage_fails_closed_when_the_ai_never_gives_usable_json():
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="not json at all"):
        assert check_template_coverage("anything") is None


def test_coverage_fails_closed_when_the_ai_call_fails():
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        assert check_template_coverage("anything") is None
