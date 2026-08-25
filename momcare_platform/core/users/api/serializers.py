from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.db import transaction
from django.utils.encoding import DjangoUnicodeDecodeError
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers

from momcare_platform.core.organization.models import LICENSE_AUTHORITY_CHOICES, Organization
from momcare_platform.core.users.models import Role, User


class RegisterSerializer(serializers.Serializer):
    # Step 1 — personal details
    first_name = serializers.CharField(max_length=50)
    last_name = serializers.CharField(max_length=50)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    gender = serializers.ChoiceField(choices=User.GENDER_CHOICES, required=False, allow_blank=True, default="")

    # Step 2 — org identity
    org_name = serializers.CharField(max_length=255)

    # Step 3 — org contact
    org_email = serializers.EmailField()
    org_phone = serializers.CharField(max_length=20)

    # Step 4 — org location
    address_line1 = serializers.CharField(max_length=200)
    address_line2 = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    city = serializers.CharField(max_length=100)
    state = serializers.CharField(max_length=100)
    postal_code = serializers.CharField(max_length=20)
    country = serializers.CharField(max_length=100)
    license_no = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    license_authority = serializers.ChoiceField(
        choices=LICENSE_AUTHORITY_CHOICES,
        required=False,
        allow_blank=True,
        default="",
    )

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value.lower()

    def validate_password(self, value):
        validate_password(value)
        return value

    def validate_phone(self, value):
        if value and User.objects.filter(phone=value).exists():
            raise serializers.ValidationError("This phone number is already registered.")
        return value

    @transaction.atomic
    def create(self, validated_data):
        role = Role.objects.get(code=settings.ROLE_HOSPITAL_ADMIN)

        user = User.objects.create_user(
            email=validated_data["email"],
            password=validated_data["password"],
            first_name=validated_data["first_name"],
            last_name=validated_data["last_name"],
            phone=validated_data.get("phone") or None,
            gender=validated_data.get("gender", ""),
            role=role,
        )

        org = Organization.objects.create(
            name=validated_data["org_name"],
            owner=user,
            phone=validated_data["org_phone"],
            email=validated_data["org_email"],
            address_line1=validated_data["address_line1"],
            address_line2=validated_data.get("address_line2", ""),
            city=validated_data["city"],
            state=validated_data["state"],
            postal_code=validated_data["postal_code"],
            country=validated_data["country"],
            license_no=validated_data.get("license_no", ""),
            license_authority=validated_data.get("license_authority", ""),
        )

        user.organization = org
        user.save(update_fields=["organization", "updated_at"])

        return user


class UserMeSerializer(serializers.ModelSerializer):
    role_code = serializers.CharField(read_only=True)
    organization_name = serializers.CharField(source="organization.name", read_only=True)

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "phone",
            "gender",
            "role_code",
            "organization_id",
            "organization_name",
            "is_email_verified",
            "created_at",
        ]
        read_only_fields = fields


class PasswordChangeSerializer(serializers.Serializer):
    """Changing your own password, while signed in.

    The current password is required even though the request is authenticated.
    An access token proves the session was authenticated at some point, not that
    the person holding the laptop right now is its owner — an unlocked screen is
    enough to take an account over otherwise.
    """

    current_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_current_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("That is not your current password.")
        return value

    def validate_new_password(self, value):
        # Django's configured validators - length, commonness, similarity to the
        # user's own details. Run against the user so the similarity check has
        # something to compare with.
        validate_password(value, user=self.context["request"].user)
        return value

    def validate(self, attrs):
        if attrs["current_password"] == attrs["new_password"]:
            raise serializers.ValidationError(
                {"new_password": "The new password must be different from the current one."},
            )
        return attrs


class PasswordResetRequestSerializer(serializers.Serializer):
    """Asking for a reset link.

    Nothing here reveals whether the address is registered — see the view.
    """

    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    """Setting a new password from a link.

    ``uid`` and ``token`` come from the emailed URL. The token is Django's own
    reset token: signed rather than stored, derived from the user's current
    password hash and last login, so it stops working the moment it is used or
    the password changes by any other route. That is single-use without a table
    to keep, and without a token sitting in the database waiting to be stolen.
    """

    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        try:
            user_id = urlsafe_base64_decode(attrs["uid"]).decode()
            user = User.objects.get(pk=user_id)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist, DjangoUnicodeDecodeError):
            raise serializers.ValidationError(
                {"detail": "This reset link is not valid. Request a new one."},
            ) from None

        if not default_token_generator.check_token(user, attrs["token"]):
            raise serializers.ValidationError(
                {"detail": "This reset link has expired or has already been used."},
            )

        validate_password(attrs["new_password"], user=user)
        attrs["user"] = user
        return attrs
