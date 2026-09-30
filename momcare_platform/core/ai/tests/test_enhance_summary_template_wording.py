"""enhance_summary_template_wording() -- the admin builds the layout by
hand (we already decided 17 fields is simple enough not to need AI for
that); this only ever polishes the *wording* they've already drafted,
never the structure. Constrained to the fixed 17-field vocabulary (never
invent a fact to mention) and to the platform's configured word limit.

Two real bugs reported from live usage and reproduced before this fix:
(1) a blank draft made the AI invent a ~47-word block that literally
listed every field name in prose -- reading exactly like a miniature
summary, not a short instruction, because the old code explicitly asked
it to "suggest a short, useful starting point" from nothing. (2) a
genuinely short draft (3 words) got expanded to 10+ words because the
prompt's only stated ceiling was the platform's 150-word summary limit,
giving the model no signal to stay close to the original length."""

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


def test_a_blank_draft_never_fabricates_wording():
    """Real bug: a blank draft used to ask the AI to "suggest a short,
    useful starting point," which produced invented multi-sentence
    guidance out of nothing -- the same "never invent" rule that governs
    the real summary applies here too. A blank draft has nothing to
    enhance, so it's stated plainly instead."""
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Preview.") as mock_generate:
        result = enhance_summary_template_wording(_full_sections(), "")

    assert result is not None
    assert result["extra_instructions"] == ""
    assert result["word_count"] == 0
    assert "message" in result
    # Only the preview call happens -- no wording-enhance call is made at
    # all when there's nothing to enhance.
    assert mock_generate.call_count == 1


def test_a_blank_draft_still_returns_a_preview():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value="Jane Sample is stable overall.",
    ):
        result = enhance_summary_template_wording(_full_sections(), "   ")

    assert result is not None
    assert result["preview_text"] == "Jane Sample is stable overall."


def test_a_short_draft_is_not_expanded_far_beyond_its_own_length():
    """Real bug: a 3-word draft ("mention adherence always") got expanded
    to 10+ words because the prompt's only stated ceiling was the
    platform's 150-word summary limit -- no signal to stay close to the
    original length. The prompt must tell the model how long the
    original draft was."""
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=["ok", "preview"],
    ) as mock_generate:
        enhance_summary_template_wording(_full_sections(), "mention adherence always")

    enhance_prompt = mock_generate.call_args_list[0].args[0]
    assert "3 words" in enhance_prompt


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
