"""AIProviderConfig singleton -- get_ai_config() is the only read path."""

import pytest
from django.db import IntegrityError, transaction

from momcare_platform.core.ai.models import AIProviderConfig
from momcare_platform.core.ai.services import get_ai_config

pytestmark = pytest.mark.django_db


def test_get_ai_config_creates_the_row_from_settings_on_first_call(settings):
    settings.MOMCARE_AI_SUMMARY_DEFAULT_MODEL = "google/gemini-2.0-flash-001"
    settings.MOMCARE_AI_SUMMARY_DEFAULT_MAX_WORDS = 150
    assert AIProviderConfig.objects.count() == 0

    config = get_ai_config()

    assert config.current_model == "google/gemini-2.0-flash-001"
    assert config.max_words == 150
    assert AIProviderConfig.objects.count() == 1


def test_get_ai_config_returns_the_existing_row_on_later_calls():
    AIProviderConfig.objects.create(current_model="deepseek/deepseek-chat", max_words=120)

    config = get_ai_config()

    assert config.current_model == "deepseek/deepseek-chat"
    assert AIProviderConfig.objects.count() == 1


def test_only_one_ai_provider_config_row_can_ever_exist():
    """Important review finding: AIProviderConfig had no true singleton
    guarantee -- two concurrent first-callers of get_ai_config() could each
    see zero rows and each create one, leaving two rows that a later
    .first() call would pick between non-deterministically."""
    AIProviderConfig.objects.create(current_model="google/gemini-2.0-flash-001", max_words=150)

    with pytest.raises(IntegrityError), transaction.atomic():
        AIProviderConfig.objects.create(current_model="deepseek/deepseek-chat", max_words=120)

    assert AIProviderConfig.objects.count() == 1
