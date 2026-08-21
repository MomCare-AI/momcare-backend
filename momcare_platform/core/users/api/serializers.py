from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
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
