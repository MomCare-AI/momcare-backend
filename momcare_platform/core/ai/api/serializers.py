from rest_framework import serializers

from momcare_platform.core.ai.models import AISummary


class AISummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = AISummary
        fields = ["content", "generated_at", "model_used"]
