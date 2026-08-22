from rest_framework import serializers

from momcare_platform.core.alerts import escalation
from momcare_platform.core.alerts.models import Alert, AlertEvent


class AlertEventSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    actor_name = serializers.CharField(source="actor.get_full_name", read_only=True, default="")
    tier_label = serializers.SerializerMethodField()

    class Meta:
        model = AlertEvent
        fields = ["id", "kind", "kind_display", "tier", "tier_label", "detail", "actor_name", "created_at"]
        read_only_fields = fields

    def get_tier_label(self, obj) -> str:
        return escalation.tier_label(obj.tier) if obj.tier else ""


class AlertSerializer(serializers.ModelSerializer):
    """One row of the alert list.

    Carries the patient inline. The alert list is read under time pressure and
    an extra request per row to find out *who* would make it useless.
    """

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    resolution_display = serializers.CharField(source="get_resolution_display", read_only=True)
    tier_label = serializers.CharField(read_only=True)
    next_escalation_at = serializers.DateTimeField(read_only=True)
    reasons = serializers.ListField(read_only=True)

    patient_id = serializers.UUIDField(source="pregnancy.patient.id", read_only=True)
    pregnancy_id = serializers.UUIDField(source="pregnancy.id", read_only=True)
    patient_name = serializers.CharField(source="pregnancy.patient.full_name", read_only=True)
    mrn = serializers.CharField(source="pregnancy.patient.mrn", read_only=True, default="")
    gestational_age = serializers.CharField(
        source="pregnancy.gestational_age_display",
        read_only=True,
        default="",
    )
    assigned_staff_name = serializers.SerializerMethodField()
    acknowledged_by_name = serializers.CharField(
        source="acknowledged_by.get_full_name",
        read_only=True,
        default="",
    )

    class Meta:
        model = Alert
        fields = [
            "id",
            "level",
            "status",
            "status_display",
            "tier",
            "tier_label",
            "reasons",
            "raised_at",
            "next_escalation_at",
            "last_escalated_at",
            "acknowledged_at",
            "acknowledged_by_name",
            "resolved_at",
            "resolution",
            "resolution_display",
            "patient_id",
            "pregnancy_id",
            "patient_name",
            "mrn",
            "gestational_age",
            "assigned_staff_name",
        ]
        read_only_fields = fields

    def get_assigned_staff_name(self, obj) -> str:
        """Empty when nobody is responsible — which the interface must show.

        A soft-deleted clinician still leaves the foreign key populated, so an
        inactive one is reported as no clinician rather than as cover.
        """
        staff = obj.pregnancy.assigned_staff
        if staff and staff.is_active and staff.user:
            return staff.user.get_full_name()
        return ""


class AlertDetailSerializer(AlertSerializer):
    """The alert plus its full history — who was told, when, and who answered."""

    events = AlertEventSerializer(many=True, read_only=True)

    class Meta(AlertSerializer.Meta):
        fields = [*AlertSerializer.Meta.fields, "events"]
        read_only_fields = fields


class ResolveSerializer(serializers.Serializer):
    resolution = serializers.ChoiceField(
        choices=Alert.RESOLUTION_CHOICES,
        default=Alert.RESOLUTION_HANDLED,
    )
