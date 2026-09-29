from rest_framework import serializers

from momcare_platform.core.ai.models import AISummary, AISummaryTemplate
from momcare_platform.core.ai.services import get_ai_config, validate_template_sections


class AISummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = AISummary
        fields = ["content", "generated_at", "model_used", "citations"]


class AISummaryTemplateSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True, default="")
    extra_instructions_word_count = serializers.SerializerMethodField()

    class Meta:
        model = AISummaryTemplate
        fields = [
            "id",
            "name",
            "sections",
            "extra_instructions",
            "extra_instructions_word_count",
            "is_active",
            "activated_at",
            "created_at",
            "created_by_name",
        ]
        read_only_fields = [
            "is_active",
            "activated_at",
            "created_at",
            "created_by_name",
            "extra_instructions_word_count",
        ]

    def get_extra_instructions_word_count(self, obj) -> int:
        return len(obj.extra_instructions.split())

    def validate_sections(self, value):
        errors = validate_template_sections(value)
        if errors:
            raise serializers.ValidationError(errors)
        return value

    def validate_extra_instructions(self, value):
        """extra_instructions competes with the rest of the prompt for the
        same word budget (see _BASE_PROMPT's "keep the entire summary to at
        most {max_words} words" instruction) -- text that alone already
        exceeds the platform's configured max_words can never fit, so it's
        rejected here rather than silently accepted and producing a summary
        that's over budget before the model even starts on the real data."""
        limit = get_ai_config().max_words
        word_count = len(value.split())
        if word_count > limit:
            raise serializers.ValidationError(
                f"Extra instructions use {word_count} words, but the platform limit is {limit}. "
                f"Please shorten it to {limit} words or fewer.",
            )
        return value


class SummaryTemplateEnhanceRequestSerializer(serializers.Serializer):
    """Input to the AI-assisted wording step -- the admin's own hand-built
    sections (validated the same way the real create endpoint validates
    them) plus whatever extra_instructions draft they've written so far,
    which may be blank. Not tied to a model: nothing here is ever saved
    directly, it only drives one enhance_summary_template_wording() call."""

    sections = serializers.JSONField()
    extra_instructions = serializers.CharField(allow_blank=True, max_length=2000, required=False, default="")

    def validate_sections(self, value):
        errors = validate_template_sections(value)
        if errors:
            raise serializers.ValidationError(errors)
        return value
