"""generate_with_retries() -- openrouter_client.generate() can come back
None (a real transport failure) or, with a reasoning-style model, a
non-None but blank/whitespace string (the model spent its whole token
budget on internal reasoning and never wrote a visible answer). Both are
equally unusable, but unlike a genuine transport failure, retrying a
blank-content response often succeeds -- how much a reasoning model
"thinks" before answering varies call to call for the same prompt. Caught
live: 4 of 5 real attempts against the summary prompt came back blank."""

from unittest.mock import patch

import pytest

from momcare_platform.core.ai.services import generate_with_retries

pytestmark = pytest.mark.django_db


def test_returns_content_on_the_first_successful_attempt():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value="A real summary.",
    ) as mock_generate:
        content = generate_with_retries("prompt", model="m", max_tokens=100)

    assert content == "A real summary."
    assert mock_generate.call_count == 1


def test_retries_past_a_blank_response_and_succeeds():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=[" ", "", "A real summary."],
    ) as mock_generate:
        content = generate_with_retries("prompt", model="m", max_tokens=100)

    assert content == "A real summary."
    assert mock_generate.call_count == 3


def test_retries_past_a_none_response_and_succeeds():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=[None, "A real summary."],
    ) as mock_generate:
        content = generate_with_retries("prompt", model="m", max_tokens=100)

    assert content == "A real summary."
    assert mock_generate.call_count == 2


def test_gives_up_and_returns_none_after_every_attempt_is_blank():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value="   ",
    ) as mock_generate:
        content = generate_with_retries("prompt", model="m", max_tokens=100)

    assert content is None
    assert mock_generate.call_count > 1


def test_gives_up_and_returns_none_after_every_attempt_is_none():
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value=None,
    ) as mock_generate:
        content = generate_with_retries("prompt", model="m", max_tokens=100)

    assert content is None
    assert mock_generate.call_count > 1
