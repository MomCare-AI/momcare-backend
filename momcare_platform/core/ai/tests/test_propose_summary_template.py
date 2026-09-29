"""propose_summary_template()/propose_and_preview_template() -- the
AI-assisted authoring loop. An admin describes what they want in plain
English; the AI proposes a candidate {sections, extra_instructions}, which
is run through the exact same validate_template_sections() check a
hand-built template gets -- an invalid proposal is retried automatically
(bounded) rather than ever reaching the caller. Nothing is saved anywhere;
this only ever produces a draft."""

import json
from unittest.mock import patch

import pytest

from momcare_platform.core.ai.services import (
    TEMPLATE_FIELD_VOCABULARY,
    propose_and_preview_template,
    propose_summary_template,
)

pytestmark = pytest.mark.django_db


def _valid_ai_response(extra_instructions=""):
    return json.dumps(
        {
            "sections": [{"label": "All Fields", "fields": list(TEMPLATE_FIELD_VOCABULARY)}],
            "extra_instructions": extra_instructions,
        },
    )


def test_propose_returns_a_valid_candidate_when_the_ai_responds_with_valid_json():
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=_valid_ai_response()):
        candidate = propose_summary_template("put everything in one section")

    assert candidate is not None
    assert candidate["sections"] == [{"label": "All Fields", "fields": list(TEMPLATE_FIELD_VOCABULARY)}]
    assert candidate["extra_instructions"] == ""


def test_propose_passes_through_extra_instructions_the_ai_proposed():
    response = _valid_ai_response(extra_instructions="Always mention medication adherence.")
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=response):
        candidate = propose_summary_template("mention medication adherence always")

    assert candidate is not None
    assert candidate["extra_instructions"] == "Always mention medication adherence."


def test_propose_strips_markdown_code_fences_from_the_response():
    fenced = f"```json\n{_valid_ai_response()}\n```"
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=fenced):
        candidate = propose_summary_template("anything")

    assert candidate is not None
    assert candidate["sections"] == [{"label": "All Fields", "fields": list(TEMPLATE_FIELD_VOCABULARY)}]


def test_propose_retries_on_invalid_content_and_succeeds_on_a_later_attempt():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=["not json at all", _valid_ai_response()],
    ) as mock_generate:
        candidate = propose_summary_template("anything")

    assert candidate is not None
    assert mock_generate.call_count == 2


def test_propose_returns_none_after_exhausting_retries_on_invalid_content():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value="not json at all",
    ) as mock_generate:
        candidate = propose_summary_template("anything")

    assert candidate is None
    assert mock_generate.call_count > 1


def test_propose_rejects_a_response_missing_a_field_and_retries():
    incomplete = json.dumps(
        {
            "sections": [
                {"label": "Incomplete", "fields": [f for f in TEMPLATE_FIELD_VOCABULARY if f != "patient_name"]},
            ],
            "extra_instructions": "",
        },
    )
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=[incomplete, _valid_ai_response()],
    ) as mock_generate:
        candidate = propose_summary_template("anything")

    assert candidate is not None
    assert mock_generate.call_count == 2


def test_propose_returns_none_immediately_when_the_client_call_fails():
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None) as mock_generate:
        candidate = propose_summary_template("anything")

    assert candidate is None
    assert mock_generate.call_count == 1


def test_propose_and_preview_includes_preview_text_generated_from_sample_data():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=[_valid_ai_response(), "This is a sample preview of the summary."],
    ) as mock_generate:
        result = propose_and_preview_template("anything")

    assert result is not None
    assert result["preview_text"] == "This is a sample preview of the summary."
    assert result["sections"] == [{"label": "All Fields", "fields": list(TEMPLATE_FIELD_VOCABULARY)}]
    assert mock_generate.call_count == 2


def test_propose_and_preview_still_returns_the_candidate_when_the_preview_call_fails():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=[_valid_ai_response(), None],
    ):
        result = propose_and_preview_template("anything")

    assert result is not None
    assert result["preview_text"] is None
    assert result["sections"] == [{"label": "All Fields", "fields": list(TEMPLATE_FIELD_VOCABULARY)}]


def test_propose_and_preview_returns_none_when_the_proposal_itself_fails():
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        result = propose_and_preview_template("anything")

    assert result is None


def test_propose_and_preview_never_uses_a_real_patient():
    """No patient_id or organization is ever passed in -- the preview is
    generated purely from fixed sample data, at either tier."""
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=[_valid_ai_response(), "preview"],
    ) as mock_generate:
        propose_and_preview_template("anything")

    preview_prompt = mock_generate.call_args_list[1].args[0]
    assert "Jane Sample" in preview_prompt
