from django.conf import settings
from rest_framework import serializers

from momcare_platform.core.patients.models import Patient, PatientJoinRequest, Pregnancy
from momcare_platform.core.staff.api.serializers import SecondaryProviderBriefSerializer
from momcare_platform.core.staff.models import Staff


class PregnancySerializer(serializers.ModelSerializer):
    """Gestational age is exposed but never accepted — it is derived from the
    EDD on every read, so the client and the risk engine cannot disagree."""

    gestational_age_weeks = serializers.SerializerMethodField()
    gestational_age_days = serializers.SerializerMethodField()
    gestational_age_display = serializers.CharField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    edd_source_display = serializers.CharField(source="get_edd_source_display", read_only=True)
    provider_name = serializers.CharField(
        source="provider.user.get_full_name",
        read_only=True,
        default="",
    )
    nurse_name = serializers.CharField(
        source="nurse.user.get_full_name",
        read_only=True,
        default="",
    )
    care_manager_name = serializers.CharField(
        source="care_manager.user.get_full_name",
        read_only=True,
        default="",
    )
    # Surfaced so the UI can warn: an assignment to someone who has left the
    # hospital is as good as no assignment once alerts start routing.
    has_responsible_clinician = serializers.BooleanField(read_only=True)
    provider_is_active = serializers.BooleanField(
        source="provider.is_active",
        read_only=True,
        default=False,
    )
    present_factors = serializers.ListField(read_only=True)
    unanswered_factors = serializers.ListField(read_only=True)

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
            "provider",
            "provider_name",
            "provider_is_active",
            "nurse",
            "nurse_name",
            "care_manager",
            "care_manager_name",
            "has_responsible_clinician",
            "status",
            "status_display",
            "outcome_date",
            "notes",
            *Pregnancy.FACTOR_FIELDS,
            "present_factors",
            "unanswered_factors",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "patient",
            "edd_confirmed_at",
            "provider_is_active",
            "has_responsible_clinician",
            "gestational_age_weeks",
            "gestational_age_days",
            "gestational_age_display",
            "present_factors",
            "unanswered_factors",
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
    """Create/update, allowing risk factors and the care team to be set
    alongside the pregnancy.

    Every care-team assignment is checked three ways here, so that
    onboarding, opening a later pregnancy, and correcting one afterwards all
    go through identical rules rather than three drifting copies:

    - **Same hospital** — structural, via ``OrganizationStaffField``, whose
      queryset is narrowed per-request. A clinician from another hospital
      fails as "does not exist", never confirming they exist elsewhere.
    - **Role matches the slot** — a nurse cannot hold the provider slot.
      Without this the column would claim an accountability the person
      doesn't have.
    - **Capacity** — refuses someone already at ``max_patients``, so a
      caseload limit is a real limit rather than a number nobody enforces.
    """

    provider = OrganizationStaffField(required=False, allow_null=True)
    nurse = OrganizationStaffField(required=False, allow_null=True)
    care_manager = OrganizationStaffField(required=False, allow_null=True)

    # The slot name and the role code are deliberately the same string.
    ROLE_FOR_FIELD = {
        "provider": settings.ROLE_PROVIDER,
        "nurse": settings.ROLE_NURSE,
        "care_manager": settings.ROLE_CARE_MANAGER,
    }

    class Meta(PregnancySerializer.Meta):
        read_only_fields = PregnancySerializer.Meta.read_only_fields

    def validate(self, attrs):
        attrs = super().validate(attrs)

        errors = {}
        for field_name, role_code in self.ROLE_FOR_FIELD.items():
            if field_name not in attrs:
                continue
            staff = attrs[field_name]
            if staff is None:
                continue

            # Re-submitting the assignment a pregnancy already has is not a
            # new assignment. Without this, saving an unrelated field on a
            # pregnancy whose clinician has since filled up would fail on a
            # capacity check nobody was asking it to make.
            if self.instance is not None and getattr(self.instance, f"{field_name}_id", None) == staff.pk:
                continue

            if staff.user.role_code != role_code:
                errors[field_name] = f"This staff member is not a {role_code.replace('_', ' ')}."
                continue

            # Locked for the check: two concurrent assignments must not both
            # read "one slot left" and both take it.
            locked = Staff.objects.select_for_update().get(pk=staff.pk)
            if not locked.has_capacity:
                errors[field_name] = "This staff member is already at their patient capacity."

        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class PatientListSerializer(serializers.ModelSerializer):
    """Deliberately lean — a list view should not carry a whole clinical record.

    It does carry the current risk level, because that is what decides which
    row a clinician opens first. Without it the list is alphabetical noise and
    the deteriorating patient sits between two stable ones.
    """

    full_name = serializers.CharField(read_only=True)
    pregnancy_id = serializers.SerializerMethodField()
    gestational_age_display = serializers.SerializerMethodField()
    pregnancy_status = serializers.SerializerMethodField()
    risk_level = serializers.SerializerMethodField()
    risk_assessed_at = serializers.SerializerMethodField()
    statuses = serializers.SerializerMethodField()

    class Meta:
        model = Patient
        fields = [
            "id",
            "mrn",
            "full_name",
            "phone",
            "cnic",
            "date_of_birth",
            "pregnancy_id",
            "gestational_age_display",
            "pregnancy_status",
            "risk_level",
            "risk_assessed_at",
            "statuses",
            "is_active",
            "created_at",
        ]
        read_only_fields = fields

    def _pregnancy(self, obj):
        """Prefer the prefetched active pregnancy over a fresh query.

        ``current_pregnancy`` hits the database once per patient, which on a
        paginated list is one query per row. The list view prefetches into
        ``active_pregnancies``; anything that has not done so still works, just
        without the saving.
        """
        prefetched = getattr(obj, "active_pregnancies", None)
        if prefetched is not None:
            return prefetched[0] if prefetched else None
        return obj.current_pregnancy

    def get_pregnancy_id(self, obj) -> str | None:
        pregnancy = self._pregnancy(obj)
        return str(pregnancy.id) if pregnancy else None

    def get_gestational_age_display(self, obj) -> str | None:
        pregnancy = self._pregnancy(obj)
        return pregnancy.gestational_age_display if pregnancy else None

    def get_pregnancy_status(self, obj) -> str | None:
        pregnancy = self._pregnancy(obj)
        return pregnancy.status if pregnancy else None

    def get_risk_level(self, obj) -> str | None:
        """None means never assessed — which is not the same as stable, and the
        interface has to keep the two apart."""
        pregnancy = self._pregnancy(obj)
        return getattr(pregnancy, "latest_risk_level", None) if pregnancy else None

    def get_risk_assessed_at(self, obj) -> str | None:
        pregnancy = self._pregnancy(obj)
        assessed_at = getattr(pregnancy, "latest_risk_at", None) if pregnancy else None
        return assessed_at.isoformat() if assessed_at else None

    def _statuses(self, obj):
        """Prefer the prefetched queryset over a fresh per-row query -- same
        reasoning as ``_pregnancy`` above. Full history, newest first
        (``PatientStatus.Meta.ordering``), matching Neuro_RPM's own
        unconditional embed of every entry -- see the design doc's Decision 3."""
        prefetched = getattr(obj, "prefetched_statuses", None)
        if prefetched is not None:
            return prefetched
        return obj.statuses.all()

    def get_statuses(self, obj) -> list[dict]:
        return [{"name": s.name, "description": s.description, "color": s.color} for s in self._statuses(obj)]


class PatientDetailSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    has_app_account = serializers.BooleanField(read_only=True)
    current_pregnancy = PregnancySerializer(read_only=True)
    location_name = serializers.CharField(source="location.name", read_only=True)
    secondary_provider_detail = SecondaryProviderBriefSerializer(source="secondary_provider", read_only=True)
    statuses = serializers.SerializerMethodField()

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
            "emergency_contact_email",
            "has_app_account",
            "location_name",
            "secondary_provider_detail",
            "current_pregnancy",
            "consent_date",
            "secondary_provider",
            "secondary_provider_detail",
            "statuses",
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
            "secondary_provider_detail",
            "current_pregnancy",
            "statuses",
            "created_at",
            "updated_at",
        ]

    def get_statuses(self, obj) -> list[dict]:
        """Full history, newest first -- same shape as ``PatientListSerializer``'s
        own ``get_statuses``, matching Neuro_RPM's unconditional embed (design
        doc Decision 3)."""
        return [{"name": s.name, "description": s.description, "color": s.color} for s in obj.statuses.all()]


class PatientCreateSerializer(serializers.Serializer):
    """Onboarding: the person, and optionally her current pregnancy.

    Location is never accepted from the client — it is resolved from the
    caller's own hospital, so onboarding cannot place a patient in another
    tenant.
    """

    # Supplied by the hospital, never generated. Hospitals arrive with their
    # own existing numbering (on paper files, lab slips, an old system), and a
    # number this platform invented would be a second, competing identifier
    # for the same woman. Optional: a hospital without one leaves it blank.
    mrn = serializers.CharField(
        max_length=100,
        required=False,
        allow_null=True,
        allow_blank=True,
        default=None,
    )
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
    emergency_contact_email = serializers.EmailField(required=False, allow_blank=True, default="")
    # When she agreed to be monitored. Optional — a hospital that records
    # consent on paper leaves it blank rather than inventing a date.
    consent_date = serializers.DateField(required=False, allow_null=True, default=None)

    # allow_null on both optional blocks, not just required=False: a client
    # that builds the whole object and sets the absent parts to null (the
    # normal way a JS or Dart frontend expresses "no pregnancy yet") means
    # exactly the same thing as omitting the key, and should not be a 400.
    pregnancy = PregnancyWriteSerializer(required=False, allow_null=True)

    PATIENT_FIELDS = [
        "mrn",
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
        "emergency_contact_email",
        "consent_date",
    ]

    def validate_mrn(self, value):
        # "" becomes None so the database stores NULL, never an empty string —
        # otherwise two MRN-less patients would collide under the unique
        # constraint. Only coercion happens here; the uniqueness check lives
        # in validate() below.
        return value or None

    def validate_cnic(self, value):
        return value or None

    def validate(self, attrs):
        # Every applicable problem is collected into one dict and raised once,
        # rather than failing on the first: a caller fixing a duplicate MRN
        # shouldn't then discover a duplicate CNIC on the next round trip.
        errors = {}

        mrn = attrs.get("mrn")
        if mrn and Patient.objects.filter(mrn__iexact=mrn).exists():
            errors["mrn"] = ["A patient with this MRN already exists."]

        cnic = attrs.get("cnic")
        organization_id = getattr(self.context["request"].user, "organization_id", None)
        # A caller with no hospital (platform_admin) is skipped rather than
        # filtered on organization_id=None: that lookup would ask for patients
        # whose hospital is NULL, which is a column Patient cannot hold, so it
        # would quietly pass every duplicate CNIC instead of catching one.
        # The database's unique_cnic_per_organization constraint is the real
        # guarantee; this check exists to turn it into a clean 400 first.
        if cnic and organization_id is not None:
            if Patient.objects.filter(organization_id=organization_id, cnic__iexact=cnic).exists():
                errors["cnic"] = ["A patient with this CNIC is already registered at this hospital."]

        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def split(self) -> tuple[dict, dict | None]:
        data = self.validated_data
        patient_data = {k: v for k, v in data.items() if k in self.PATIENT_FIELDS}
        pregnancy = data.get("pregnancy")
        return patient_data, pregnancy


class WorklistReasonSerializer(serializers.Serializer):
    """One administrative/care-continuity gap — never a bare code, always a
    sentence a clinician can act on without opening the record first."""

    code = serializers.CharField()
    detail = serializers.CharField()
    days = serializers.IntegerField(allow_null=True)


class WorklistPatientSerializer(serializers.Serializer):
    """One row of the worklist — see docs/worklist-feature-scope.md.

    Deliberately separate from AttentionPatientSerializer: this list answers
    "does this case have a gap unrelated to today's vitals," not "is this
    patient clinically at-risk right now." Never merged into one shape.
    """

    patient_id = serializers.UUIDField()
    pregnancy_id = serializers.UUIDField()
    full_name = serializers.CharField()
    gestational_age = serializers.CharField()
    reasons = WorklistReasonSerializer(many=True)


class PatientDraftSerializer(serializers.Serializer):
    """What a woman can say about herself before any hospital has her.

    Deliberately a subset of ``PatientCreateSerializer``: no location, no
    organization, no care team, no MRN. Every one of those is a hospital's
    decision, and she has no hospital yet — offering the fields would invite
    a client to send values that are silently ignored.
    """

    first_name = serializers.CharField(max_length=50)
    last_name = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    cnic = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    blood_group = serializers.CharField(max_length=3, required=False, allow_blank=True, default="")
    emergency_contact_name = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    emergency_contact_phone = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    emergency_contact_relation = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
    emergency_contact_email = serializers.EmailField(required=False, allow_blank=True, default="")
    consent_date = serializers.DateField(required=False, allow_null=True)

    # Her pregnancy as she reports it — dating and history only. Status and
    # outcome belong to a clinician.
    lmp = serializers.DateField(required=False, allow_null=True)
    edd = serializers.DateField(required=False, allow_null=True)
    gravida = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    para = serializers.IntegerField(required=False, allow_null=True, min_value=0)

    PATIENT_FIELDS = [
        "first_name",
        "last_name",
        "date_of_birth",
        "phone",
        "cnic",
        "blood_group",
        "emergency_contact_name",
        "emergency_contact_phone",
        "emergency_contact_relation",
        "emergency_contact_email",
        "consent_date",
    ]
    PREGNANCY_FIELDS = ["lmp", "edd", "gravida", "para", *Pregnancy.FACTOR_FIELDS]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The seven obstetric answers, declared once from the model so this
        # never drifts from Pregnancy.FACTOR_FIELDS.
        for field in Pregnancy.FACTOR_FIELDS:
            self.fields[field] = serializers.ChoiceField(
                choices=Pregnancy.ANSWER_CHOICES,
                required=False,
                default=Pregnancy.UNKNOWN,
            )

    def split(self) -> tuple[dict, dict | None]:
        """Into the two dicts ``onboard_patient`` takes."""
        data = self.validated_data
        patient_data = {k: v for k, v in data.items() if k in self.PATIENT_FIELDS}
        pregnancy_data = {k: v for k, v in data.items() if k in self.PREGNANCY_FIELDS}
        # A pregnancy needs a start; without either date there is nothing to
        # open and she is simply registered as a patient.
        if not (pregnancy_data.get("lmp") or pregnancy_data.get("edd")):
            return patient_data, None
        return patient_data, pregnancy_data


class PatientJoinRequestSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    organization_city = serializers.CharField(source="organization.city", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    patient_id = serializers.UUIDField(source="patient.id", read_only=True, default=None)

    class Meta:
        model = PatientJoinRequest
        fields = [
            "id",
            "organization",
            "organization_name",
            "organization_city",
            "status",
            "status_display",
            "draft",
            "decision_note",
            "decided_at",
            "patient_id",
            "created_at",
        ]
        read_only_fields = fields
