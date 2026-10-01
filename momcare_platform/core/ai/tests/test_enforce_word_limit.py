"""The word limit applies to the summary a patient page shows, not just to the
AI's instructions."""

from unittest.mock import patch

import pytest

from momcare_platform.core.ai.models import AISummary
from momcare_platform.core.ai.services import (
    _trim_to_word_limit,
    enforce_word_limit,
    generate_patient_summary,
    get_ai_config,
)
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db


def _words(n, stop_every=10):
    return " ".join(("word." if (i + 1) % stop_every == 0 else "word") for i in range(n))


def test_a_summary_within_the_limit_is_untouched_and_no_ai_call_is_made():
    with patch("momcare_platform.core.ai.openrouter_client.generate") as gen:
        assert enforce_word_limit(_words(130), get_ai_config()) == _words(130)

    gen.assert_not_called()


def test_an_over_limit_summary_is_shortened_by_the_ai():
    shorter = _words(120)
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=shorter) as gen:
        result = enforce_word_limit(_words(160), get_ai_config())

    assert result == shorter
    assert gen.call_count == 1
    assert "at most 130 words" in gen.call_args.args[0]


def test_if_the_ai_keeps_going_over_it_is_trimmed_at_a_sentence_boundary():
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=_words(150)):
        result = enforce_word_limit(_words(160), get_ai_config())

    assert len(result.split()) <= 130
    assert result.endswith(".")


def test_if_the_ai_fails_the_limit_is_still_enforced():
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        result = enforce_word_limit(_words(200), get_ai_config())

    assert len(result.split()) <= 130


def test_trim_never_exceeds_the_limit_even_with_no_sentence_end():
    assert len(_trim_to_word_limit(" ".join(["word"] * 300), 130).split()) == 130


def test_the_saved_patient_summary_never_exceeds_the_limit(make_hospital):
    hospital = make_hospital("Word Limit Enforcement Hospital")
    patient = onboard_patient(organization=hospital.org, patient_data={"first_name": "Amina", "last_name": "Yousaf"})

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=_words(170)):
        generate_patient_summary(patient)

    assert len(AISummary.objects.get(patient=patient).content.split()) <= 130
