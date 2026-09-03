from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from momcare_platform.core.common.rls import bypass_rls
from momcare_platform.core.staff.models import Staff, StaffInvite
from momcare_platform.core.staff.services import INVITABLE_ROLE_CODES
from momcare_platform.core.users.models import Role, User


class StaffMemberSerializer(serializers.ModelSerializer):
    """A person on the hospital's team, flattened for the staff list."""

    full_name = serializers.CharField(source="user.get_full_name", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    role_code = serializers.CharField(source="user.role_code", read_only=True)
    role_name = serializers.CharField(source="user.role.name", read_only=True, default="")
    is_user_active = serializers.BooleanField(source="user.is_active", read_only=True)
    years_of_experience = serializers.IntegerField(read_only=True)

    class Meta:
        model = Staff
        fields = [
            "id",
            "employee_id",
            "full_name",
            "email",
            "role_code",
            "role_name",
            "is_user_active",
            "is_active",
            "photo",
            "qualifications",
            "specialty",
            "registration_number",
            "registration_authority",
            "practicing_since",
            "years_of_experience",
            "created_at",
        ]
        read_only_fields = fields


class StaffProfileUpdateSerializer(serializers.ModelSerializer):
    """The credentialing fields a person keeps current about themselves —
    self-reported, not verified against any registry, same honesty rule as
    everywhere else self-reported data appears in this codebase. Writable by
    the staff member themselves or by their hospital_admin; never touches
    employee_id, role, or anything tenant-membership-related."""

    class Meta:
        model = Staff
        fields = [
            "photo",
            "qualifications",
            "specialty",
            "registration_number",
            "registration_authority",
            "practicing_since",
        ]


class StaffInviteSerializer(serializers.ModelSerializer):
    """Read view of an invite. ``token`` is included so the admin can copy the
    link and send it over WhatsApp — email is not the only delivery channel."""

    role_code = serializers.CharField(source="role.code", read_only=True)
    role_name = serializers.CharField(source="role.name", read_only=True)
    invited_by_name = serializers.CharField(source="invited_by.get_full_name", read_only=True, default="")
    status = serializers.CharField(read_only=True)

    class Meta:
        model = StaffInvite
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "role_code",
            "role_name",
            "token",
            "status",
            "expires_at",
            "invited_by_name",
            "accepted_at",
            "created_at",
        ]
        read_only_fields = fields


class StaffInviteCreateSerializer(serializers.Serializer):
    """Create an invite. Organization comes from the requesting admin, never
    from the payload, so an admin cannot invite into someone else's hospital."""

    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
    last_name = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
    role_code = serializers.ChoiceField(choices=sorted(INVITABLE_ROLE_CODES))
    # Optional by design: clinical staff here often reach each other by WhatsApp
    # rather than email, so the admin may prefer to copy the link and send it
    # themselves. The invitation is created either way.
    send_email = serializers.BooleanField(required=False, default=True)

    def validate_email(self, value):
        value = value.lower().strip()
        # Deliberately platform-wide, not scoped to the inviting hospital:
        # email is the global sign-in identity (a real unique constraint on
        # User.email), so a collision at a *different* hospital is exactly
        # what this exists to catch before it becomes a raw IntegrityError
        # at invite-acceptance time instead of a clean validation error now.
        with bypass_rls():
            if User.objects.filter(email__iexact=value).exists():
                raise serializers.ValidationError("Someone with this email already has an account.")
        return value

    def validate(self, attrs):
        org = self.context["organization"]
        if StaffInvite.objects.filter(
            organization=org,
            email__iexact=attrs["email"],
            accepted_at__isnull=True,
            revoked_at__isnull=True,
        ).exists():
            raise serializers.ValidationError(
                {"email": "There is already a pending invitation for this email."},
            )
        return attrs

    def create(self, validated_data):
        org = self.context["organization"]
        return StaffInvite.objects.create(
            organization=org,
            email=validated_data["email"],
            first_name=validated_data.get("first_name", ""),
            last_name=validated_data.get("last_name", ""),
            role=Role.objects.get(code=validated_data["role_code"]),
            invited_by=self.context["request"].user,
        )

    def validate_role_code(self, value):
        # ChoiceField already restricts this; the explicit check documents *why*
        # platform_admin and patient are absent — no privilege escalation, and
        # patients are enrolled clinically rather than invited onto the team.
        if value not in INVITABLE_ROLE_CODES:
            raise serializers.ValidationError("That role cannot be invited into a hospital.")
        return value


class InvitePreviewSerializer(serializers.ModelSerializer):
    """What an unauthenticated recipient may see before accepting.

    Deliberately narrow: enough to know the invite is genuine and who sent it,
    with nothing about the hospital's other staff or its review history.
    """

    organization_name = serializers.CharField(source="organization.name", read_only=True)
    role_name = serializers.CharField(source="role.name", read_only=True)
    invited_by_name = serializers.CharField(source="invited_by.get_full_name", read_only=True, default="")

    class Meta:
        model = StaffInvite
        fields = [
            "email",
            "first_name",
            "last_name",
            "organization_name",
            "role_name",
            "invited_by_name",
            "expires_at",
        ]
        read_only_fields = fields


class InviteAcceptSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=50)
    last_name = serializers.CharField(max_length=50)
    password = serializers.CharField(write_only=True, min_length=8)

    def validate_password(self, value):
        validate_password(value)
        return value
