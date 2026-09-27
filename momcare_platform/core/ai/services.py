from django.conf import settings

from momcare_platform.core.ai.models import AIProviderConfig


def get_ai_config() -> AIProviderConfig:
    """The single read path for AIProviderConfig -- creates the row from
    settings on first call, since none exists yet in a fresh database."""
    config = AIProviderConfig.objects.first()
    if config is not None:
        return config
    return AIProviderConfig.objects.create(
        current_model=settings.MOMCARE_AI_SUMMARY_DEFAULT_MODEL,
        max_words=settings.MOMCARE_AI_SUMMARY_DEFAULT_MAX_WORDS,
    )
