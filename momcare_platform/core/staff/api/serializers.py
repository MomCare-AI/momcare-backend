from django.conf import settings
from rest_framework import serializers

from momcare_platform.core.common.rls import bypass_rls
from momcare_platform.core.locations.models import Location
from momcare_platform.core.staff.models import SecondaryProvider, Staff
from momcare_platform.core.staff.services import STAFF_ROLE_CODES
from momcare_platform.core.users.models import Role, User


class StaffMemberSerializer(serializers.ModelSerializer):
    """A person on the hospital's team, flattened for the staff list."""

    full_name = serializers.CharField(source="user.get_full_name", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    phone = serializers.CharField(source="user.phone", read_only=True, default="")
    role_code = serializers.CharField(source="user.role_code", read_only=True)
    role_name = serializers.CharField(source="user.role.name", read_only=True, default="")
    is_user_active = serializers.BooleanField(source="user.is_active", read_only=True)
    years_of_experience = serializers.IntegerField(read_only=True)
    location_ids = serializers.PrimaryKeyRelatedField(source="user.locations", many=True, read_only=True)
    # False until they follow the invitation link and choose a password. An
    # account that exists but has never been activated looks identical to a
    # working one otherwise, which is exactly the thing an admin needs to see
    # when a new nurse says she cannot sign in.
    has_activated = serializers.SerializerMethodField()

    def get_has_activated(self, obj) -> bool:
        return not obj.user.requires_password_reset

    class Meta:
        model = Staff
        fields = [
            "id",
            "employee_id",
            "full_name",
            "email",
            "phone",
            "role_code",
            "role_name",
            "is_user_active",
            "has_activated",
            "is_active",
            "photo",
            "qualifications",
            "specialty",
            "registration_number",
            "registration_authority",
            "practicing_since",
            "years_of_experience",
            "location_ids",
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


class OrgLocationsField(serializers.PrimaryKeyRelatedField):
    """Locations selectable when onboarding staff — restricted to the
    requesting user's own hospital, same shape as ``LocationManagerField``
    in ``locations/api/serializers.py``."""

    def get_queryset(self):
        user = self.context["request"].user
        organization_id = getattr(user, "organization_id", None)
        if organization_id is None:
            return Location.objects.none()
        return Location.objects.filter(organization_id=organization_id)


class StaffOnboardSerializer(serializers.Serializer):
    """Create a hospital staff member's account and invite them to activate it.

    No password is accepted here, by design. The account is created with an
    unusable password and MomCare emails a one-time link; the staff member
    chooses their own password. Nobody else — not even the admin who created
    the account — ever knows it, which is what makes "this clinician
    acknowledged the alert" provable rather than merely recorded.

    ``locations`` is compulsory for provider/nurse/care_manager — every
    clinical staff member is accountable to at least one site — and optional
    for hospital_admin, who already sees the whole hospital regardless.
    Whoever isn't hospital_admin may only assign locations they themselves
    manage (``Location.location_manager_id == the caller``) — checked in
    ``validate()``, not permissions, since it depends on *which* locations
    were actually submitted, not just who's asking.
    """

    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
    last_name = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
    # Contact information, not a credential — sign-in is by email only (see
    # LoginView). This is how a hospital reaches a clinician about an alert
    # off-portal, so it is worth holding even though nothing authenticates
    # against it.
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    role_code = serializers.ChoiceField(choices=sorted(STAFF_ROLE_CODES))
    locations = OrgLocationsField(many=True, required=False, default=list)

    def validate_email(self, value):
        value = value.lower().strip()
        # Deliberately platform-wide, not scoped to the onboarding hospital:
        # email is the global sign-in identity (a real unique constraint on
        # User.email), so a collision at a *different* hospital is exactly
        # what this exists to catch before it becomes a raw IntegrityError.
        with bypass_rls():
            if User.objects.filter(email__iexact=value).exists():
                raise serializers.ValidationError("Someone with this email already has an account.")
        return value

    def validate_phone(self, value):
        # Platform-wide for the same reason as email: User.phone carries a
        # real unique constraint, so a number already held at another
        # hospital must surface as a 400 rather than an IntegrityError.
        value = value.strip()
        if not value:
            return value
        with bypass_rls():
            if User.objects.filter(phone=value).exists():
                raise serializers.ValidationError("Someone with this phone number already has an account.")
        return value

    def validate_role_code(self, value):
        # ChoiceField already restricts this; the explicit check documents *why*
        # platform_admin and patient are absent — no privilege escalation, and
        # patients are enrolled clinically rather than onboarded onto the team.
        if value not in STAFF_ROLE_CODES:
            raise serializers.ValidationError("That role cannot be onboarded directly.")
        return value

    def validate(self, attrs):
        role_code = attrs["role_code"]
        locations = attrs.get("locations") or []
        requester = self.context["request"].user
        requester_is_admin = requester.role_code == settings.ROLE_HOSPITAL_ADMIN

        # A location manager's "locations optional for hospital_admin" carve-out
        # is for hospital_admin *onboarding*, not for who may be onboarded as
        # one — a non-admin submitting role_code=hospital_admin with an empty
        # locations list would otherwise skip both checks below entirely and
        # mint a brand-new admin account with no ownership check ever applying.
        if not requester_is_admin and role_code == settings.ROLE_HOSPITAL_ADMIN:
            raise serializers.ValidationError(
                {"role_code": "Only a hospital administrator can onboard another hospital administrator."},
            )

        if role_code != settings.ROLE_HOSPITAL_ADMIN and not locations:
            raise serializers.ValidationError(
                {"locations": "At least one location is required for this role."},
            )

        if not requester_is_admin:
            not_managed = [loc for loc in locations if loc.location_manager_id != requester.id]
            if not_managed:
                raise serializers.ValidationError(
                    {"locations": "You can only onboard staff into locations you manage."},
                )
        return attrs


class StaffUpdateSerializer(serializers.Serializer):
    """Update an existing staff member — the admin-level counterpart to
    ``StaffProfileUpdateSerializer``'s self-service credentialing-only PATCH.

    Every field is optional (a PATCH omitting one leaves it untouched,
    same convention as ``LocationSerializer``): a caller wanting to reset
    just a specialty does not have to resend the person's role and locations
    too. Only callable by hospital_admin or a manager of (at least one of)
    this staff member's locations — enforced in the view via
    ``services.can_manage_staff`` — with the same escalation guard used at
    onboarding: a non-admin can never touch ``role_code=hospital_admin``,
    and may only assign locations they themselves manage.
    """

    email = serializers.EmailField(required=False)
    first_name = serializers.CharField(max_length=50, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=50, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    role_code = serializers.ChoiceField(choices=sorted(STAFF_ROLE_CODES), required=False)
    locations = OrgLocationsField(many=True, required=False)
    photo = serializers.FileField(required=False, allow_null=True)
    qualifications = serializers.CharField(max_length=255, required=False, allow_blank=True)
    specialty = serializers.CharField(max_length=150, required=False, allow_blank=True)
    registration_number = serializers.CharField(max_length=100, required=False, allow_blank=True)
    registration_authority = serializers.CharField(max_length=150, required=False, allow_blank=True)
    practicing_since = serializers.DateField(required=False, allow_null=True)

    def validate_email(self, value):
        value = value.lower().strip()
        with bypass_rls():
            if User.objects.filter(email__iexact=value).exclude(pk=self.instance.user_id).exists():
                raise serializers.ValidationError("Someone with this email already has an account.")
        return value

    def validate_phone(self, value):
        # ``exclude(self)`` so resubmitting a PATCH that carries the person's
        # own unchanged number is not rejected as a duplicate of themselves.
        value = value.strip()
        if not value:
            return value
        with bypass_rls():
            if User.objects.filter(phone=value).exclude(pk=self.instance.user_id).exists():
                raise serializers.ValidationError("Someone with this phone number already has an account.")
        return value

    def validate_role_code(self, value):
        if value not in STAFF_ROLE_CODES:
            raise serializers.ValidationError("Staff cannot hold that role.")
        return value

    def validate(self, attrs):
        requester = self.context["request"].user
        requester_is_admin = requester.role_code == settings.ROLE_HOSPITAL_ADMIN
        effective_role_code = attrs.get("role_code", self.instance.user.role_code)

        if not requester_is_admin and attrs.get("role_code") == settings.ROLE_HOSPITAL_ADMIN:
            raise serializers.ValidationError(
                {"role_code": "Only a hospital administrator can promote someone to hospital administrator."},
            )

        if "locations" in attrs:
            locations = attrs["locations"]
            if effective_role_code != settings.ROLE_HOSPITAL_ADMIN and not locations:
                raise serializers.ValidationError(
                    {"locations": "At least one location is required for this role."},
                )
            if not requester_is_admin:
                not_managed = [loc for loc in locations if loc.location_manager_id != requester.id]
                if not_managed:
                    raise serializers.ValidationError(
                        {"locations": "You can only assign locations you manage."},
                    )
        return attrs

    def update(self, instance, validated_data):
        user = instance.user

        locations = validated_data.pop("locations", None)

        user_fields = [f for f in ("email", "first_name", "last_name", "phone") if f in validated_data]
        for field in user_fields:
            value = validated_data[field]
            # Clearing a phone must store NULL, not "". User.phone is unique,
            # and "" collides with the next staff member who also has none,
            # while any number of NULLs coexist happily.
            if field == "phone" and not value:
                value = None
            setattr(user, field, value)
        if "role_code" in validated_data:
            user.role = Role.objects.get(code=validated_data["role_code"])
            user_fields.append("role_code")
        if user_fields:
            user.save()

        if locations is not None:
            user.locations.set(locations)

        staff_fields = [
            f
            for f in (
                "photo",
                "qualifications",
                "specialty",
                "registration_number",
                "registration_authority",
                "practicing_since",
            )
            if f in validated_data
        ]
        for field in staff_fields:
            setattr(instance, field, validated_data[field])
        if staff_fields:
            instance.save(update_fields=[*staff_fields, "updated_at"])

        instance.refresh_from_db()
        return instance


class SecondaryProviderSerializer(serializers.ModelSerializer):
    """An external clinician. ``organization`` is never accepted from the
    client — it comes from the authenticated user, so one hospital can't file
    a referral contact into another's list."""

    patient_count = serializers.SerializerMethodField()

    class Meta:
        model = SecondaryProvider
        fields = [
            "id",
            "name",
            "email",
            "phone",
            "affiliation",
            "patient_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "patient_count", "created_at", "updated_at"]

    def get_patient_count(self, obj) -> int:
        """How many patients reference this clinician — the whole reason this
        is a shared record rather than columns on each patient."""
        return obj.patients.count()

    def validate_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("A name is required.")
        return value.strip()


class SecondaryProviderBriefSerializer(serializers.ModelSerializer):
    """Nested read-only shape, for showing the referring clinician on a patient."""

    class Meta:
        model = SecondaryProvider
        fields = ["id", "name", "email", "phone", "affiliation"]
