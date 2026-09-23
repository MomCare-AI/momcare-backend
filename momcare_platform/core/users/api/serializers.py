from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.db import transaction
from django.utils.encoding import DjangoUnicodeDecodeError
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers

from momcare_platform.core.common.mail import send_email_otp
from momcare_platform.core.common.rls import bypass_rls
from momcare_platform.core.organization.models import Organization
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
    # Evidence a platform admin checks before approving the application —
    # see Organization.license_number's own field comment. Required: an
    # application with nothing to verify against isn't one a reviewer can
    # actually act on.
    license_number = serializers.CharField(max_length=100)

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
            license_number=validated_data["license_number"],
            owner=user,
            phone=validated_data["org_phone"],
            email=validated_data["org_email"],
            address_line1=validated_data["address_line1"],
            address_line2=validated_data.get("address_line2", ""),
            city=validated_data["city"],
            state=validated_data["state"],
            postal_code=validated_data["postal_code"],
            country=validated_data["country"],
        )

        user.organization = org
        user.save(update_fields=["organization", "updated_at"])

        return user


class UserMeSerializer(serializers.ModelSerializer):
    role_code = serializers.CharField(read_only=True)
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    # None for a platform_admin or a patient - neither has a Staff row. The
    # frontend needs this to know whether "me" matches a given care-team
    # row's staff id, e.g. to decide whether to show that pregnancy's write
    # controls without a second round-trip.
    staff_id = serializers.SerializerMethodField()

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
            "staff_id",
            "is_email_verified",
            # True until an invited staff member has chosen their own
            # password. The frontend uses this to send them straight to the
            # set-password screen instead of a half-usable portal.
            "requires_password_reset",
            "created_at",
        ]
        read_only_fields = fields

    def get_staff_id(self, obj):
        staff = getattr(obj, "staff", None)
        return str(staff.id) if staff else None


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


def _resolve_reset_token_user(uid: str, token: str) -> User:
    """Shared by ResetPasswordSerializer and VerifyResetTokenSerializer — both
    need the same uid+token resolution, but only one of them also changes the
    password. Raises the same generic error either way: expired, tampered,
    and already-used all look identical from the outside, on purpose.
    """
    try:
        user_id = urlsafe_base64_decode(uid).decode()
        user = User.objects.get(pk=user_id)
    except TypeError, ValueError, OverflowError, User.DoesNotExist, DjangoUnicodeDecodeError:
        raise serializers.ValidationError(
            {"detail": "This reset link is not valid. Request a new one."},
        ) from None

    if not default_token_generator.check_token(user, token):
        raise serializers.ValidationError(
            {"detail": "This reset link has expired or has already been used."},
        )
    return user


class ForgotPasswordSerializer(serializers.Serializer):
    """Asking for a reset link.

    Nothing here reveals whether the address is registered — see the view.
    """

    email = serializers.EmailField()


class VerifyResetTokenSerializer(serializers.Serializer):
    """Lets the frontend check a reset link is still good *before* showing the
    new-password form, instead of only finding out after the user fills it in
    and submits. Purely read-only — never changes anything. ResetPasswordView
    still re-validates the token itself regardless of what this said (the link
    could expire in the gap between the two calls), so this is a UX layer on
    top of that check, not a second source of truth.
    """

    uid = serializers.CharField()
    token = serializers.CharField()

    def validate(self, attrs):
        attrs["user"] = _resolve_reset_token_user(attrs["uid"], attrs["token"])
        return attrs


class ResetPasswordSerializer(serializers.Serializer):
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
        user = _resolve_reset_token_user(attrs["uid"], attrs["token"])
        validate_password(attrs["new_password"], user=user)
        attrs["user"] = user
        return attrs


class PatientRegisterSerializer(serializers.Serializer):
    """A woman creating her own account from the mobile app.

    She picks her own password here — unlike staff, there is no hospital to
    invite her, so there is nobody to send a link. Her account is created with
    **no organization**: she belongs to no hospital until one approves her
    join request, and until then there is no Patient row either.

    The account starts unverified (``is_email_verified=False``) and no
    tokens are issued from this endpoint — see ``PatientRegisterView``. An
    email OTP has to be confirmed before she can sign in at all.
    """

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)
    first_name = serializers.CharField(max_length=50)
    last_name = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")

    def validate_email(self, value):
        value = value.lower().strip()
        # Platform-wide, like staff onboarding: email is the global sign-in
        # identity, so a collision anywhere is what this exists to catch.
        # An unverified PATIENT row is the one exception — she may have
        # mistyped a field, lost the code, or let it expire, and a single
        # abandoned attempt must not permanently claim an email she never
        # actually got to use. create() below deletes that stale row before
        # making the fresh one.
        #
        # Every OTHER account — verified, or any non-patient role — blocks
        # the address for good. ``is_email_verified`` is never set for staff
        # or hospital owners at all (they're identity-checked a different
        # way), so without the role filter here a "retry" would try to
        # delete a real hospital admin's account to make room for a patient
        # signup — and 500 the moment that admin owns an Organization
        # (a protected FK), instead of failing as a clean validation error.
        with bypass_rls():
            blocked = User.objects.filter(email__iexact=value).exclude(
                role__code=settings.ROLE_PATIENT,
                is_email_verified=False,
            )
            if blocked.exists():
                raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_password(self, value):
        validate_password(value)
        return value

    @transaction.atomic
    def create(self, validated_data):
        from momcare_platform.core.users.models import EmailVerificationCode

        with bypass_rls():
            User.objects.filter(
                email__iexact=validated_data["email"],
                role__code=settings.ROLE_PATIENT,
                is_email_verified=False,
            ).delete()

            user = User.objects.create_user(
                email=validated_data["email"],
                password=validated_data["password"],
                first_name=validated_data["first_name"],
                last_name=validated_data.get("last_name", ""),
                phone=validated_data.get("phone") or None,
                role=Role.objects.get(code=settings.ROLE_PATIENT),
            )

        _, code = EmailVerificationCode.issue(user)
        send_email_otp(user, code)
        return user
