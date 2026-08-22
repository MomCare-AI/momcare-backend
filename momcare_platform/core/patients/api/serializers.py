from rest_framework import serializers

from momcare_platform.core.patients.models import Consent, Patient, Pregnancy, PregnancyRiskFactors
from momcare_platform.core.staff.models import Staff


class PregnancyRiskFactorsSerializer(serializers.ModelSerializer):
    present_factors = serializers.ListField(read_only=True)
    unanswered_factors = serializers.ListField(read_only=True)

    class Meta:
        model = PregnancyRiskFactors
        fields = [
            "id",
            *PregnancyRiskFactors.FACTOR_FIELDS,
            "present_factors",
            "unanswered_factors",
            "updated_at",
        ]
        read_only_fields = ["id", "present_factors", "unanswered_factors", "updated_at"]


class PregnancySerializer(serializers.ModelSerializer):
    """Gestational age is exposed but never accepted — it is derived from the
    EDD on every read, so the client and the risk engine cannot disagree."""

    gestational_age_weeks = serializers.SerializerMethodField()
    gestational_age_days = serializers.SerializerMethodField()
    gestational_age_display = serializers.CharField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    edd_source_display = serializers.CharField(source="get_edd_source_display", read_only=True)
    assigned_staff_name = serializers.CharField(
        source="assigned_staff.user.get_full_name",
        read_only=True,
        default="",
    )
    # Surfaced so the UI can warn: an assignment to someone who has left the
    # hospital is as good as no assignment once alerts start routing.
    has_responsible_clinician = serializers.BooleanField(read_only=True)
    assigned_staff_is_active = serializers.BooleanField(
        source="assigned_staff.is_active",
        read_only=True,
        default=False,
    )
    risk_factors = PregnancyRiskFactorsSerializer(read_only=True)

    class Meta:
        model = Pregnancy
        fields = [
            "id",
            "patient",
            "lmp",
            "edd",
            "edd_source",
            "edd_source_display",
            "edd_confirmed_at",
            "gestational_age_weeks",
            "gestational_age_days",
            "gestational_age_display",
            "gravida",
            "para",
            "assigned_staff",
            "assigned_staff_name",
            "assigned_staff_is_active",
            "has_responsible_clinician",
            "status",
            "status_display",
            "outcome_date",
            "notes",
            "risk_factors",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "patient",
            "edd_confirmed_at",
            "assigned_staff_is_active",
            "has_responsible_clinician",
            "gestational_age_weeks",
            "gestational_age_days",
            "gestational_age_display",
            "risk_factors",
            "created_at",
            "updated_at",
        ]

    def get_gestational_age_weeks(self, obj) -> int | None:
        age = obj.gestational_age
        return age.weeks if age else None

    def get_gestational_age_days(self, obj) -> int | None:
        age = obj.gestational_age
        return age.days if age else None

    def validate(self, attrs):
        lmp = attrs.get("lmp", getattr(self.instance, "lmp", None))
        edd = attrs.get("edd", getattr(self.instance, "edd", None))
        if lmp is None and edd is None:
            raise serializers.ValidationError(
                "Provide either a last menstrual period or an estimated delivery date — "
                "without one, gestational age cannot be calculated and no reading can be "
                "interpreted.",
            )
        return attrs


class OrganizationStaffField(serializers.PrimaryKeyRelatedField):
    """A Staff reference restricted to the requesting user's own hospital.

    A plain PrimaryKeyRelatedField accepts any Staff id on the platform, so a
    hospital admin could name another hospital's clinician as responsible for
    their patient — leaking that clinician's existence and making the
    accountability record false. The queryset is narrowed per-request instead,
    which also means the API never depends on the dropdown having been filtered
    correctly.
    """

    def get_queryset(self):
        user = self.context["request"].user
        organization_id = getattr(user, "organization_id", None)
        if organization_id is None:
            return Staff.objects.none()
        return Staff.objects.filter(user__organization_id=organization_id, is_active=True)


class PregnancyWriteSerializer(PregnancySerializer):
    """Create/update, allowing risk factors to be set alongside the pregnancy."""

    risk_factors = PregnancyRiskFactorsSerializer(required=False)
    assigned_staff = OrganizationStaffField(required=False, allow_null=True)

    class Meta(PregnancySerializer.Meta):
        read_only_fields = [
            f for f in PregnancySerializer.Meta.read_only_fields if f != "risk_factors"
        ]

    def update(self, instance, validated_data):
        factors = validated_data.pop("risk_factors", None)
        pregnancy = super().update(instance, validated_data)
        if factors:
            PregnancyRiskFactors.objects.update_or_create(pregnancy=pregnancy, defaults=factors)
        return pregnancy


class ConsentSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    method_display = serializers.CharField(source="get_method_display", read_only=True)
    recorded_by_name = serializers.CharField(
        source="recorded_by.get_full_name",
        read_only=True,
        default="",
    )

    class Meta:
        model = Consent
        fields = [
            "id",
            "status",
            "status_display",
            "recorded_at",
            "version",
            "method",
            "method_display",
            "recorded_by_name",
            "note",
        ]
        read_only_fields = ["id", "recorded_at", "recorded_by_name", "status_display", "method_display"]


class PatientListSerializer(serializers.ModelSerializer):
    """Deliberately lean — a list view should not carry a whole clinical record."""

    full_name = serializers.CharField(read_only=True)
    gestational_age_display = serializers.SerializerMethodField()
    pregnancy_status = serializers.SerializerMethodField()

    class Meta:
        model = Patient
        fields = [
            "id",
            "mrn",
            "full_name",
            "phone",
            "cnic",
            "date_of_birth",
            "gestational_age_display",
            "pregnancy_status",
            "is_active",
            "created_at",
        ]
        read_only_fields = fields

    def get_gestational_age_display(self, obj) -> str | None:
        pregnancy = obj.current_pregnancy
        return pregnancy.gestational_age_display if pregnancy else None

    def get_pregnancy_status(self, obj) -> str | None:
        pregnancy = obj.current_pregnancy
        return pregnancy.status if pregnancy else None


class PatientDetailSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    has_app_account = serializers.BooleanField(read_only=True)
    current_pregnancy = PregnancySerializer(read_only=True)
    consents = ConsentSerializer(many=True, read_only=True)
    location_name = serializers.CharField(source="location.name", read_only=True)

    class Meta:
        model = Patient
        fields = [
            "id",
            "mrn",
            "first_name",
            "last_name",
            "full_name",
            "date_of_birth",
            "gender",
            "phone",
            "cnic",
            "blood_group",
            "emergency_contact_name",
            "emergency_contact_phone",
            "emergency_contact_relation",
            "has_app_account",
            "location_name",
            "current_pregnancy",
            "consents",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "mrn",
            "full_name",
            "has_app_account",
            "location_name",
            "current_pregnancy",
            "consents",
            "created_at",
            "updated_at",
        ]


class ConsentInputSerializer(serializers.Serializer):
    """Consent captured at enrolment. Required, because storing a patient's
    record without a recorded agreement is not something the API should allow."""

    status = serializers.ChoiceField(choices=Consent.STATUS_CHOICES, default=Consent.STATUS_GRANTED)
    version = serializers.CharField(max_length=20, default="v1.0")
    method = serializers.ChoiceField(choices=Consent.METHOD_CHOICES, default=Consent.METHOD_IN_PERSON)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class PatientCreateSerializer(serializers.Serializer):
    """Enrolment: the person, optionally her current pregnancy, and consent.

    Location is never accepted from the client — it is resolved from the
    caller's own hospital, so enrolment cannot place a patient in another
    tenant.
    """

    first_name = serializers.CharField(max_length=50)
    last_name = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    gender = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    cnic = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    blood_group = serializers.CharField(max_length=3, required=False, allow_blank=True, default="")
    emergency_contact_name = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    emergency_contact_phone = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    emergency_contact_relation = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")

    pregnancy = PregnancyWriteSerializer(required=False)
    consent = ConsentInputSerializer()

    PATIENT_FIELDS = [
        "first_name",
        "last_name",
        "date_of_birth",
        "gender",
        "phone",
        "cnic",
        "blood_group",
        "emergency_contact_name",
        "emergency_contact_phone",
        "emergency_contact_relation",
    ]

    def split(self) -> tuple[dict, dict | None, dict | None, dict]:
        data = self.validated_data
        patient_data = {k: v for k, v in data.items() if k in self.PATIENT_FIELDS}
        pregnancy = data.get("pregnancy")
        risk_factors = pregnancy.pop("risk_factors", None) if pregnancy else None
        return patient_data, pregnancy, risk_factors, data["consent"]
