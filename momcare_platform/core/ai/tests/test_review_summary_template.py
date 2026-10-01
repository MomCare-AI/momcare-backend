"""review_summary_template() -- the platform admin's review step. Missing
fields come back as an alert (nothing generated); a complete template gets
the entire summary rendered from sample data."""

from unittest.mock import patch

import pytest

from momcare_platform.core.ai.services import TEMPLATE_FIELD_VOCABULARY, review_summary_template

pytestmark = pytest.mark.django_db


def _full_sections():
    return [{"label": "All Fields", "fields": list(TEMPLATE_FIELD_VOCABULARY)}]


def test_a_complete_template_returns_the_full_preview():
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="A full sample summary.") as gen:
        result = review_summary_template(_full_sections())

    assert result is not None
    assert result["complete"] is True
    assert result["missing_fields"] == []
    assert result["preview_text"] == "A full sample summary."
    assert result["word_count"] == 4
    assert result["word_limit"] == 150
    assert "All Fields:" in gen.call_args.args[0]


def test_missing_fields_are_reported_and_nothing_is_generated():
    sections = [{"label": "Partial", "fields": ["patient_name", "gestational_age"]}]

    with patch("momcare_platform.core.ai.openrouter_client.generate") as gen:
        result = review_summary_template(sections)

    gen.assert_not_called()
    assert result is not None
    assert result["complete"] is False
    assert result["preview_text"] is None
    expected = [f for f in TEMPLATE_FIELD_VOCABULARY if f not in ("patient_name", "gestational_age")]
    assert result["missing_fields"] == expected
    assert "provider_name" in result["message"]


def test_a_failed_preview_returns_none():
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        assert review_summary_template(_full_sections()) is None
