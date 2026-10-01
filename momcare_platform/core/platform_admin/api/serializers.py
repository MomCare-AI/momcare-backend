"""Platform-admin API serializers. AIProviderConfig is the first real one --
see api/views.py for why this app existed as an empty skeleton until now."""

from rest_framework import serializers

from momcare_platform.core.ai import openrouter_client
from momcare_platform.core.ai.models import AIProviderConfig


class AIProviderConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIProviderConfig
        fields = ["current_model", "max_words"]

    def validate_current_model(self, value):
        # Module-attribute call, not `from ... import list_available_models` --
        # the latter would bind this module's own reference at import time,
        # which a test's `patch("...openrouter_client.list_available_models")`
        # could no longer reach. Same pattern core.ai.services already uses
        # for openrouter_client.generate().
        catalog = openrouter_client.list_available_models()
        if catalog is None:
            # OpenRouter's catalog is briefly unreachable -- fail closed
            # rather than silently accepting a value we can't verify.
            raise serializers.ValidationError("The service is not working right now. Please try again later.")
        valid_ids = {entry["id"] for entry in catalog}
        if value not in valid_ids:
            raise serializers.ValidationError(f"'{value}' is not a model OpenRouter currently serves.")
        return value
