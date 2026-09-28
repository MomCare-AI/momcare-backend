from rest_framework import serializers

from momcare_platform.core.ai.models import AIInstructionPreset, AISummary


class AISummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = AISummary
        fields = ["content", "generated_at", "model_used"]


class AIInstructionPresetSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True, default="")

    class Meta:
        model = AIInstructionPreset
        fields = ["id", "name", "content", "is_active", "activated_at", "created_at", "created_by_name"]
        read_only_fields = ["is_active", "activated_at", "created_at", "created_by_name"]
