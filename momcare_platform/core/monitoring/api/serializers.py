from decimal import Decimal

from rest_framework import serializers

from momcare_platform.core.monitoring.models import Device, RiskAssessment, VitalReading

# The 9 vitals the risk model trains and predicts on — every reading-carrying
# field on VitalReading, kept as one list so the create serializer and the
# view stay in sync with the model without repeating the field names twice.
VITAL_FIELDS = [
    "age",
    "systolic_bp",
    "diastolic_bp",
    "heart_rate",
    "body_temp_f",
    "hemoglobin",
    "blood_glucose",
    "stress_score",
    "phys_activity_score",
]

# Physiologically-plausible bounds, not statistical bounds from any dataset —
# wide enough that a genuine extreme emergency reading is never rejected, tight
# enough to catch a data-entry mistake (a heart rate of "1000" is a typo, not
# a patient). Age and the two vitals come from documented clinical extremes
# (ICU/patient-monitor alarm-configuration ranges, glucometer measurement
# specs, and the most extreme medically documented *survived* cases); stress
# and activity score are this app's own 0-10 self-report scale, not a
# clinical measurement, so their bound is just the scale's own definition.
VITAL_BOUNDS = {
    "age": (10, 60),
    "systolic_bp": (Decimal("40"), Decimal("300")),
    "diastolic_bp": (Decimal("20"), Decimal("200")),
    "heart_rate": (Decimal("20"), Decimal("300")),
    "body_temp_f": (Decimal("80"), Decimal("115")),
    "hemoglobin": (Decimal("2"), Decimal("24")),
    "blood_glucose": (Decimal("20"), Decimal("600")),
    "stress_score": (Decimal("0"), Decimal("10")),
    "phys_activity_score": (Decimal("0"), Decimal("10")),
}


class VitalReadingSerializer(serializers.ModelSerializer):
    source_display = serializers.CharField(source="get_source_display", read_only=True)

    class Meta:
        model = VitalReading
        fields = [
            "id",
            *VITAL_FIELDS,
            "source",
            "source_display",
            "recorded_at",
            "device",
        ]
        read_only_fields = fields


class VitalReadingCreateSerializer(serializers.Serializer):
    """One reading event, from a device or entered by staff.

    Every vital is optional — a reading event does not have to carry all 9
    every time (hemoglobin in particular usually will not, since it comes
    from a monthly lab report, not the band). At least one must be present,
    or there is nothing to record.

    ``pregnancy`` is never accepted here — it comes from the URL and is scoped
    to the caller's hospital, so a reading cannot be filed against someone
    else's patient.

    Each vital is bounded to a physiologically-plausible range (see
    ``VITAL_BOUNDS``) — wide enough that a genuine extreme emergency reading
    is always accepted, tight enough to reject a data-entry mistake (a heart
    rate of "1000") at the door rather than discovering it later and needing
    to rescore.
    """

    age = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=VITAL_BOUNDS["age"][0],
        max_value=VITAL_BOUNDS["age"][1],
        error_messages={
            "min_value": "Age must be between 10 and 60 years for a pregnancy record.",
            "max_value": "Age must be between 10 and 60 years for a pregnancy record.",
        },
    )
    systolic_bp = serializers.DecimalField(
        max_digits=6,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=VITAL_BOUNDS["systolic_bp"][0],
        max_value=VITAL_BOUNDS["systolic_bp"][1],
        error_messages={
            "min_value": "Systolic BP must be between 40 and 300 mmHg. Please correct it.",
            "max_value": "Systolic BP must be between 40 and 300 mmHg. Please correct it.",
        },
    )
    diastolic_bp = serializers.DecimalField(
        max_digits=6,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=VITAL_BOUNDS["diastolic_bp"][0],
        max_value=VITAL_BOUNDS["diastolic_bp"][1],
        error_messages={
            "min_value": "Diastolic BP must be between 20 and 200 mmHg. Please correct it.",
            "max_value": "Diastolic BP must be between 20 and 200 mmHg. Please correct it.",
        },
    )
    heart_rate = serializers.DecimalField(
        max_digits=6,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=VITAL_BOUNDS["heart_rate"][0],
        max_value=VITAL_BOUNDS["heart_rate"][1],
        error_messages={
            "min_value": "Heart rate must be between 20 and 300 bpm. Please correct it.",
            "max_value": "Heart rate must be between 20 and 300 bpm. Please correct it.",
        },
    )
    body_temp_f = serializers.DecimalField(
        max_digits=6,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=VITAL_BOUNDS["body_temp_f"][0],
        max_value=VITAL_BOUNDS["body_temp_f"][1],
        error_messages={
            "min_value": "Body temperature must be between 80 and 115 °F. Please correct it.",
            "max_value": "Body temperature must be between 80 and 115 °F. Please correct it.",
        },
    )
    hemoglobin = serializers.DecimalField(
        max_digits=6,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=VITAL_BOUNDS["hemoglobin"][0],
        max_value=VITAL_BOUNDS["hemoglobin"][1],
        error_messages={
            "min_value": "Hemoglobin must be between 2 and 24 g/dL. Please correct it.",
            "max_value": "Hemoglobin must be between 2 and 24 g/dL. Please correct it.",
        },
    )
    blood_glucose = serializers.DecimalField(
        max_digits=6,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=VITAL_BOUNDS["blood_glucose"][0],
        max_value=VITAL_BOUNDS["blood_glucose"][1],
        error_messages={
            "min_value": "Blood glucose must be between 20 and 600 mg/dL. Please correct it.",
            "max_value": "Blood glucose must be between 20 and 600 mg/dL. Please correct it.",
        },
    )
    stress_score = serializers.DecimalField(
        max_digits=6,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=VITAL_BOUNDS["stress_score"][0],
        max_value=VITAL_BOUNDS["stress_score"][1],
        error_messages={
            "min_value": "Stress score must be between 0 and 10. Please correct it.",
            "max_value": "Stress score must be between 0 and 10. Please correct it.",
        },
    )
    phys_activity_score = serializers.DecimalField(
        max_digits=6,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=VITAL_BOUNDS["phys_activity_score"][0],
        max_value=VITAL_BOUNDS["phys_activity_score"][1],
        error_messages={
            "min_value": "Physical activity score must be between 0 and 10. Please correct it.",
            "max_value": "Physical activity score must be between 0 and 10. Please correct it.",
        },
    )
    # Always required, never inferred — see VitalReading's own docstring on
    # why a device being assigned does not mean these particular numbers
    # came from it.
    source = serializers.ChoiceField(choices=VitalReading.SOURCE_CHOICES)
    recorded_at = serializers.DateTimeField(required=False)

    def validate(self, attrs):
        # Both halves or neither: a systolic with no diastolic is not a blood
        # pressure, and a rule evaluating 140/? cannot decide.
        systolic = attrs.get("systolic_bp")
        diastolic = attrs.get("diastolic_bp")
        if (systolic is None) != (diastolic is None):
            raise serializers.ValidationError(
                {"diastolic_bp": "Blood pressure needs both systolic and diastolic values."},
            )
        if systolic is not None and systolic <= diastolic:
            raise serializers.ValidationError(
                {"systolic_bp": "Systolic pressure must be higher than diastolic."},
            )

        if not any(attrs.get(field) is not None for field in VITAL_FIELDS):
            raise serializers.ValidationError("At least one vital must be provided.")

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
    risk_level_display = serializers.CharField(source="get_risk_level_display", read_only=True)
    final_risk_level_display = serializers.CharField(source="get_final_risk_level_display", read_only=True)
    review_status_display = serializers.CharField(source="get_review_status_display", read_only=True)
    needs_review = serializers.BooleanField(read_only=True)
    verified_by_name = serializers.CharField(
        source="verified_by.get_full_name",
        read_only=True,
        default="",
    )
    # The vitals behind this judgement, inline — a caller reading an
    # assessment gets the reading with it, not just a reading_id it has to
    # look up separately.
    reading = VitalReadingSerializer(read_only=True)

    class Meta:
        model = RiskAssessment
        fields = [
            "id",
            "risk_level",
            "risk_level_display",
            "final_risk_level",
            "final_risk_level_display",
            "previous_risk_level",
            "confirmed_risk_level",
            "review_status",
            "review_status_display",
            "flagged_for_review",
            "reading",
            "bp_category",
            "heart_rate_category",
            "temperature_category",
            "glucose_category",
            "hemoglobin_category",
            "confidence",
            "assessed_at",
            "needs_review",
            "verified_at",
            "verified_by_name",
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
    risk_level = serializers.CharField()
    risk_level_display = serializers.CharField()
    assessed_at = serializers.DateTimeField()
    needs_review = serializers.BooleanField()
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


