from rest_framework import serializers

from momcare_platform.core.ai.models import AISummary, AISummaryTemplate
from momcare_platform.core.ai.services import template_word_count, validate_template_content


class AISummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = AISummary
        fields = ["content", "generated_at", "model_used", "citations"]


class AISummaryTemplateSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True, default="")
    word_count = serializers.SerializerMethodField()

    class Meta:
        model = AISummaryTemplate
        fields = [
            "id",
            "name",
            "content",
            "word_count",
            "is_active",
            "activated_at",
            "created_at",
            "created_by_name",
        ]
        read_only_fields = ["word_count", "is_active", "activated_at", "created_at", "created_by_name"]

    def get_word_count(self, obj) -> int:
        return template_word_count(obj.content)

    def validate_content(self, value):
        errors = validate_template_content(value)
        if errors:
            raise serializers.ValidationError(errors)
        return value


class SummaryTemplateReviewRequestSerializer(serializers.Serializer):
    """Input to the review step -- the admin's plain-language draft. Blank text
    or text over the word limit is a 400; whether every field is covered is
    judged by the AI inside the review itself and reported back as an alert.
    Nothing here is saved."""

    content = serializers.CharField(trim_whitespace=False)

    def validate_content(self, value):
        errors = validate_template_content(value)
        if errors:
            raise serializers.ValidationError(errors)
        return value
