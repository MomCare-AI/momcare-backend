"""enhance_summary_template_wording() -- the admin builds the layout by
hand (we already decided 17 fields is simple enough not to need AI for
that); this only ever polishes the *wording* they've already drafted,
never the structure. Constrained to the fixed 17-field vocabulary (never
invent a fact to mention) and to the platform's configured word limit."""

from unittest.mock import patch

import pytest

from momcare_platform.core.ai.services import (
    TEMPLATE_FIELD_VOCABULARY,
    enhance_summary_template_wording,
)

pytestmark = pytest.mark.django_db


def _full_sections(label="All Fields"):
    return [{"label": label, "fields": list(TEMPLATE_FIELD_VOCABULARY)}]


def test_returns_the_enhanced_wording_and_a_word_count():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=["Always mention medication adherence clearly.", "A sample preview paragraph."],
    ):
        result = enhance_summary_template_wording(_full_sections(), "mention medication adherence")

    assert result is not None
    assert result["extra_instructions"] == "Always mention medication adherence clearly."
    assert result["word_count"] == 5
    assert result["word_limit"] == 150
    assert result["preview_text"] == "A sample preview paragraph."


def test_sections_are_never_modified_by_enhance():
    sections = _full_sections("My Custom Layout")
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=["Polished wording.", "Preview."],
    ):
        enhance_summary_template_wording(sections, "draft")

    # The function must not mutate the caller's sections list at all.
    assert sections == _full_sections("My Custom Layout")


def test_an_empty_draft_still_produces_enhanced_wording():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=["Suggested wording from a blank start.", "Preview."],
    ):
        result = enhance_summary_template_wording(_full_sections(), "")

    assert result is not None
    assert result["extra_instructions"] == "Suggested wording from a blank start."


def test_the_prompt_lists_the_fixed_vocabulary_and_the_word_limit():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=["ok", "preview"],
    ) as mock_generate:
        enhance_summary_template_wording(_full_sections(), "draft text")

    enhance_prompt = mock_generate.call_args_list[0].args[0]
    assert "patient_name" in enhance_prompt
    assert "150" in enhance_prompt
    assert "draft text" in enhance_prompt


def test_returns_none_when_the_enhance_call_itself_fails():
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        result = enhance_summary_template_wording(_full_sections(), "draft")

    assert result is None


def test_still_returns_a_result_when_only_the_preview_call_fails():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=["Enhanced wording.", None, None, None],
    ):
        result = enhance_summary_template_wording(_full_sections(), "draft")

    assert result is not None
    assert result["extra_instructions"] == "Enhanced wording."
    assert result["preview_text"] is None


def test_the_preview_uses_fixed_sample_data_never_a_real_patient():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=["Enhanced wording.", "preview"],
    ) as mock_generate:
        enhance_summary_template_wording(_full_sections(), "draft")

    preview_prompt = mock_generate.call_args_list[1].args[0]
    assert "Jane Sample" in preview_prompt
