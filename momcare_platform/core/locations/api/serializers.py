from django.conf import settings
from rest_framework import serializers
from timezone_field.rest_framework import TimeZoneSerializerField

from momcare_platform.core.locations.models import Location
from momcare_platform.core.users.models import User

# Roles a location manager may be picked from — any hospital-side staff role,
# never a patient. Matches IsHospitalStaff's own allowed_roles; the further
# restriction to a specially-flagged sub-role (Neuro_RPM's "location admin"
# concept) is deferred — not implemented here.
_MANAGER_ROLE_CODES = frozenset(
    {
        settings.ROLE_HOSPITAL_ADMIN,
        settings.ROLE_PROVIDER,
        settings.ROLE_NURSE,
        settings.ROLE_CARE_MANAGER,
    },
)


class LocationManagerField(serializers.PrimaryKeyRelatedField):
    """A location manager reference restricted to the requesting user's own
    hospital and to hospital-staff roles — mirrors
    ``patients.api.serializers.OrganizationStaffField``'s reasoning: a plain
    PrimaryKeyRelatedField would accept any user on the platform, leaking
    another hospital's staff and letting a patient be named as a manager.
    """

    def get_queryset(self):
        user = self.context["request"].user
        organization_id = getattr(user, "organization_id", None)
        if organization_id is None:
            return User.objects.none()
        return User.objects.filter(organization_id=organization_id, role__code__in=_MANAGER_ROLE_CODES)


class LocationSerializer(serializers.ModelSerializer):
    """A hospital's own site — read by any hospital staff, written by
    hospital_admin only (enforced in the view).

    ``location_manager`` is compulsory on create — a location manager is
    required on every location, no exceptions — but optional on update, so a
    partial edit that doesn't mention it leaves the existing manager alone.
    """

    timezone = TimeZoneSerializerField(required=False)
    location_manager_name = serializers.CharField(
        source="location_manager.get_full_name",
        read_only=True,
        default="",
    )
    location_manager = LocationManagerField(required=False)
    active_patient_count = serializers.IntegerField(read_only=True)
    effective_date_format = serializers.CharField(read_only=True)

    class Meta:
        model = Location
        fields = [
            "id",
            "name",
            "timezone",
            "phone",
            "email",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "country",
            "location_manager",
            "location_manager_name",
            "date_format",
            "effective_date_format",
            "is_active",
            "deactivated_at",
            "deactivation_reason",
            "active_patient_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "location_manager_name",
            "effective_date_format",
            "is_active",
            "deactivated_at",
            "deactivation_reason",
            "active_patient_count",
            "created_at",
            "updated_at",
        ]

    def validate_name(self, value):
        # The DB constraint (uniq_location_name_per_org) is the real guard;
        # this just turns a collision into a clean 400 instead of a raw
        # IntegrityError, the same pattern GlobalStatusViewSet already uses.
        org = self.context["request"].user.organization
        qs = Location.objects.filter(organization=org, name__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(f"A location named '{value}' already exists at this hospital.")
        return value

    def validate(self, attrs):
        if not self.instance and not attrs.get("location_manager"):
            raise serializers.ValidationError(
                {"location_manager": "A location manager is required for every location."},
            )
        return attrs


class MoveLocationPatientsSerializer(serializers.Serializer):
    """Payload for the move-patients action. ``target_location_id`` is
    always required; ``patient_ids`` is required only when ``move_all`` is
    false — checked in ``validate()`` so both errors surface together
    rather than one at a time across repeated requests."""

    target_location_id = serializers.UUIDField()
    move_all = serializers.BooleanField()
    patient_ids = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)

    def validate(self, attrs):
        if not attrs["move_all"] and not attrs.get("patient_ids"):
            raise serializers.ValidationError(
                {"patient_ids": "patient_ids is required and cannot be empty when move_all is false."},
            )
        return attrs
