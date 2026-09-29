"""enhance_note_text() -- an AI-assist for a staff member drafting a
clinical note, distinct from the AI Summary feature entirely: this takes
free text a person already wrote and improves its wording, never adds or
removes a fact. Same generate_with_retries() reliability handling as
every other AI call in this system."""

from unittest.mock import patch

import pytest

from momcare_platform.core.monitoring.services import enhance_note_text

pytestmark = pytest.mark.django_db


def test_returns_the_enhanced_text():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value="Patient reports mild swelling in both ankles, otherwise stable.",
    ):
        result = enhance_note_text("pt c/o mild ankle swelling both sides, otherwise ok")

    assert result == "Patient reports mild swelling in both ankles, otherwise stable."


def test_the_prompt_instructs_the_ai_to_never_change_the_facts():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value="ok",
    ) as mock_generate:
        enhance_note_text("draft note text")

    sent_prompt = mock_generate.call_args.args[0]
    assert "draft note text" in sent_prompt
    assert "never" in sent_prompt.lower()


def test_retries_a_blank_response_and_succeeds():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=["", "Enhanced wording."],
    ) as mock_generate:
        result = enhance_note_text("draft note text")

    assert result == "Enhanced wording."
    assert mock_generate.call_count == 2


def test_returns_none_after_every_attempt_fails():
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        result = enhance_note_text("draft note text")

    assert result is None
