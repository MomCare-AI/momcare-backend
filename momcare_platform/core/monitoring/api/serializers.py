from decimal import Decimal

from rest_framework import serializers

from momcare_platform.core.monitoring.models import Device, RiskAssessment, VitalReading

# DRF compares against the field's own type, so a float here would coerce on
# every request and warn.
MIN_READING = Decimal("0.01")


class VitalReadingSerializer(serializers.ModelSerializer):
    display_value = serializers.CharField(read_only=True)
    unit = serializers.CharField(read_only=True)
    is_simulated = serializers.BooleanField(read_only=True)
    reading_type_display = serializers.CharField(source="get_reading_type_display", read_only=True)
    source_display = serializers.CharField(source="get_source_display", read_only=True)

    class Meta:
        model = VitalReading
        fields = [
            "id",
            "reading_type",
            "reading_type_display",
            "value",
            "value_secondary",
            "display_value",
            "unit",
            "recorded_at",
            "source",
            "source_display",
            "is_simulated",
            "device",
        ]
        read_only_fields = fields


class VitalReadingCreateSerializer(serializers.Serializer):
    """A single reading, from a device or entered by staff.

    ``pregnancy`` is never accepted here — it comes from the URL and is scoped
    to the caller's hospital, so a reading cannot be filed against someone
    else's patient.
    """

    reading_type = serializers.ChoiceField(choices=VitalReading.TYPE_CHOICES)
    value = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=MIN_READING)
    value_secondary = serializers.DecimalField(
        max_digits=6,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=MIN_READING,
    )
    recorded_at = serializers.DateTimeField(required=False)
    source = serializers.ChoiceField(
        choices=[VitalReading.SOURCE_DEVICE, VitalReading.SOURCE_MANUAL],
        default=VitalReading.SOURCE_MANUAL,
    )

    def validate(self, attrs):
        reading_type = attrs["reading_type"]
        secondary = attrs.get("value_secondary")

        if reading_type == VitalReading.TYPE_BLOOD_PRESSURE:
            # Both halves or neither: a systolic with no diastolic is not a
            # blood pressure, and a rule evaluating 140/? cannot decide.
            if secondary is None:
                raise serializers.ValidationError(
                    {"value_secondary": "Blood pressure needs both systolic and diastolic values."},
                )
            if attrs["value"] <= secondary:
                raise serializers.ValidationError(
                    {"value": "Systolic pressure must be higher than diastolic."},
                )
        elif secondary is not None:
            raise serializers.ValidationError(
                {"value_secondary": "Only blood pressure has a second value."},
            )

        return attrs


class DeviceSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    acquisition_display = serializers.CharField(source="get_acquisition_display", read_only=True)
    wearer_name = serializers.CharField(
        source="assigned_pregnancy.patient.full_name",
        read_only=True,
        default="",
    )
    is_assigned = serializers.BooleanField(read_only=True)

    class Meta:
        model = Device
        fields = [
            "id",
            "serial_number",
            "status",
            "status_display",
            "acquisition",
            "acquisition_display",
            "assigned_pregnancy",
            "wearer_name",
            "is_assigned",
            "assigned_at",
            "notes",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "status_display",
            "acquisition_display",
            "assigned_pregnancy",
            "wearer_name",
            "is_assigned",
            "assigned_at",
            "created_at",
        ]


class RiskAssessmentSerializer(serializers.ModelSerializer):
    level_display = serializers.CharField(source="get_level_display", read_only=True)
    source_display = serializers.CharField(source="get_source_display", read_only=True)
    reasons = serializers.ListField(read_only=True)
    needs_acknowledgement = serializers.BooleanField(read_only=True)
    acknowledged_by_name = serializers.CharField(
        source="acknowledged_by.get_full_name",
        read_only=True,
        default="",
    )

    class Meta:
        model = RiskAssessment
        fields = [
            "id",
            "level",
            "level_display",
            "previous_level",
            "findings",
            "reasons",
            "source",
            "source_display",
            "engine_version",
            "score",
            "confidence",
            "assessed_at",
            "needs_acknowledgement",
            "acknowledged_at",
            "acknowledged_by_name",
        ]
        read_only_fields = fields


class AttentionPatientSerializer(serializers.Serializer):
    """One row of the queue a clinician actually works from.

    Deliberately flat and small: this list is scanned, not read, so it carries
    only what decides whether to open the record.
    """

    patient_id = serializers.UUIDField()
    pregnancy_id = serializers.UUIDField()
    full_name = serializers.CharField()
    mrn = serializers.CharField(allow_null=True)
    gestational_age = serializers.CharField()
    level = serializers.CharField()
    level_display = serializers.CharField()
    reasons = serializers.ListField(child=serializers.CharField())
    assessed_at = serializers.DateTimeField()
    needs_acknowledgement = serializers.BooleanField()
    assigned_staff_name = serializers.CharField(allow_blank=True)
    has_responsible_clinician = serializers.BooleanField()


class DeviceAssignSerializer(serializers.Serializer):
    device_id = serializers.UUIDField()
    acquisition = serializers.ChoiceField(
        choices=Device.ACQUISITION_CHOICES,
        required=False,
        allow_blank=True,
        default="",
    )


class SimulateSerializer(serializers.Serializer):
    """Development only — generates readings so the pipeline can be exercised
    before any hardware exists. Everything it writes is marked simulated."""

    hours = serializers.IntegerField(min_value=1, max_value=168, default=24)
    elevated = serializers.BooleanField(
        default=False,
        help_text="Generate a hypertensive picture, to demonstrate risk detection.",
    )
