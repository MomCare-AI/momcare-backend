from rest_framework import serializers

from momcare_platform.core.ai.models import AISummary, AISummaryTemplate
from momcare_platform.core.ai.services import validate_template_sections


class AISummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = AISummary
        fields = ["content", "generated_at", "model_used", "citations"]


class AISummaryTemplateSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True, default="")

    class Meta:
        model = AISummaryTemplate
        fields = [
            "id",
            "name",
            "sections",
            "is_active",
            "activated_at",
            "created_at",
            "created_by_name",
        ]
        read_only_fields = ["is_active", "activated_at", "created_at", "created_by_name"]

    def validate_sections(self, value):
        errors = validate_template_sections(value)
        if errors:
            raise serializers.ValidationError(errors)
        return value


class SummaryTemplateReviewRequestSerializer(serializers.Serializer):
    """Input to the review step -- the admin's draft sections, which may still
    be missing fields (reported back as an alert, not rejected). Unknown or
    duplicated fields and malformed sections are still a 400. Nothing here is
    saved."""

    sections = serializers.JSONField()

    def validate_sections(self, value):
        errors = validate_template_sections(value, allow_missing=True)
        if errors:
            raise serializers.ValidationError(errors)
        return value
