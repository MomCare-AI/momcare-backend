"""review_summary_template() -- the platform admin's review step. The AI first
judges which of the 17 fields the plain text covers; missing ones come back as
an alert (nothing else generated); a fully covered template gets its wording
polished and the entire summary rendered from sample data."""

import json
from unittest.mock import patch

import pytest

from momcare_platform.core.ai.services import TEMPLATE_FIELD_VOCABULARY, review_summary_template

pytestmark = pytest.mark.django_db

DRAFT = "reading 120/80 first, then risk, then the patient name, and the rest"
ALL = json.dumps({"covered": list(TEMPLATE_FIELD_VOCABULARY)})


def test_a_fully_covered_template_is_checked_enhanced_rechecked_and_previewed():
    polished = "Start with the reading, then risk, then the patient name, and the rest."
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=[ALL, polished, ALL, "A full sample summary."],
    ) as gen:
        result = review_summary_template(DRAFT)

    assert result is not None
    assert result["complete"] is True
    assert result["missing_fields"] == []
    assert result["enhanced_content"] == polished
    assert result["preview_text"] == "A full sample summary."
    assert result["word_limit"] == 130
    preview_prompt = gen.call_args_list[3].args[0]
    assert polished in preview_prompt
    assert "patient_name: Jane Sample" in preview_prompt


def test_missing_fields_are_reported_in_plain_words_and_nothing_else_is_generated():
    covered = [f for f in TEMPLATE_FIELD_VOCABULARY if f not in ("nurse_name", "has_open_alert")]
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value=json.dumps({"covered": covered}),
    ) as gen:
        result = review_summary_template(DRAFT)

    assert gen.call_count == 1  # only the coverage check -- no enhance, no preview
    assert result is not None
    assert result["complete"] is False
    assert result["missing_fields"] == ["nurse_name", "has_open_alert"]
    assert result["missing_labels"] == ["the nurse's name", "whether she has an open alert"]
    assert "the nurse's name" in result["message"]
    assert result["enhanced_content"] is None
    assert result["preview_text"] is None


def test_an_enhancement_that_no_longer_covers_every_field_is_discarded():
    covered = [f for f in TEMPLATE_FIELD_VOCABULARY if f != "patient_name"]
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=[ALL, "polished but dropped the name", json.dumps({"covered": covered})],
    ):
        assert review_summary_template(DRAFT) is None


def test_an_enhancement_over_the_word_limit_is_discarded():
    too_long = " ".join(f"w{i}" for i in range(131))
    with patch("momcare_platform.core.ai.openrouter_client.generate", side_effect=[ALL, too_long]):
        assert review_summary_template(DRAFT) is None


def test_an_unverifiable_coverage_check_returns_none():
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="garbage"):
        assert review_summary_template(DRAFT) is None


def test_a_failed_enhance_call_returns_none():
    with patch("momcare_platform.core.ai.openrouter_client.generate", side_effect=[ALL, None, None, None]):
        assert review_summary_template(DRAFT) is None


def test_a_failed_preview_still_returns_the_polished_text():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=[ALL, "Polished text.", ALL, None, None, None],
    ):
        result = review_summary_template(DRAFT)

    assert result is not None
    assert result["enhanced_content"] == "Polished text."
    assert result["preview_text"] is None
