from datetime import timedelta
from typing import ClassVar

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from timezone_field import TimeZoneField

from momcare_platform.core.common.languages import SUPPORTED_LANGUAGES
from momcare_platform.core.common.models import AddressMixin, TimeStampedModel, UUIDPrimaryKeyModel
from momcare_platform.core.users.managers import UserManager


class Role(UUIDPrimaryKeyModel):
    """A platform role. Authorization keys off the stable ``code``, never the
    display ``name`` (labels may change; codes must not).

    Seeded via migration: platform_admin, hospital_admin, provider, nurse,
    care_manager, patient.
    """

    code = models.SlugField(_("code"), unique=True)
    name = models.CharField(_("name"), max_length=50)
    description = models.CharField(_("description"), max_length=255, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class User(UUIDPrimaryKeyModel, AbstractBaseUser, PermissionsMixin, AddressMixin):
    """Custom user for MomCare.

    Deliberately lean: identity + auth + universal profile only. Role- and
    module-specific data lives in separate profile models (``core.Staff``,
    ``core.Patient``) that extend the user, so this model stays a stable
    dependency other apps build on rather than bloat.

    ``organization`` is the tenant-scoping FK (Section 2/8 of the architecture
    blueprint): every role except platform_admin belongs to exactly one
    hospital. platform_admin users see across all hospitals, so this stays
    null for them — enforced at the service layer (not a DB constraint, since
    a plain FK can't express "required unless role == X").
    """

    GENDER_MALE = "male"
    GENDER_FEMALE = "female"
    GENDER_OTHER = "other"
    GENDER_UNKNOWN = "unknown"
    GENDER_CHOICES = [
        (GENDER_MALE, "Male"),
        (GENDER_FEMALE, "Female"),
        (GENDER_OTHER, "Other"),
        (GENDER_UNKNOWN, "Unknown"),
    ]

    # Identity
    email = models.EmailField(_("email address"), unique=True)
    username = models.CharField(
        _("username"),
        max_length=100,
        unique=True,
        null=True,
        blank=True,
    )

    # Universal profile
    first_name = models.CharField(_("first name"), max_length=50, blank=True)
    last_name = models.CharField(_("last name"), max_length=50, blank=True)
    phone = models.CharField(_("phone"), max_length=20, blank=True, null=True, unique=True)
    timezone = TimeZoneField(default="UTC")
    avatar = models.URLField(_("avatar"), max_length=500, blank=True)
    date_of_birth = models.DateField(_("date of birth"), null=True, blank=True)
    gender = models.CharField(_("gender"), max_length=20, choices=GENDER_CHOICES, blank=True)
    # address_line1/address_line2/city/state/postal_code/country come from AddressMixin.
    language = models.CharField(
        _("language"),
        max_length=10,
        choices=[(lang["code"], lang["name"]) for lang in SUPPORTED_LANGUAGES],
        blank=True,
        default="en",
    )

    # Tenancy — see class docstring.
    organization = models.ForeignKey(
        "organization.Organization",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="users",
    )

    # Authorization
    role = models.ForeignKey(
        "users.Role",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="users",
    )
    locations = models.ManyToManyField(
        "locations.Location",
        blank=True,
        related_name="members",
    )

    # Status / lifecycle
    is_active = models.BooleanField(_("active"), default=True)
    is_staff = models.BooleanField(_("staff status"), default=False)
    is_email_verified = models.BooleanField(default=False)
    requires_password_reset = models.BooleanField(default=False)
    last_password_change = models.DateTimeField(null=True, blank=True)

    # Audit
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Case-insensitive uniqueness at the DB layer (guards direct ORM writes
            # that bypass the manager's normalization).
            models.UniqueConstraint(Lower("email"), name="user_email_ci_unique"),
        ]
        indexes = [models.Index(fields=["last_name"])]

    def __str__(self) -> str:
        return self.email

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def get_full_name(self) -> str:
        return self.full_name or self.email

    def get_short_name(self) -> str:
        return self.first_name or self.email

    @property
    def role_code(self) -> str | None:
        return self.role.code if self.role else None


class EmailVerificationCode(UUIDPrimaryKeyModel, TimeStampedModel):
    """A one-time code proving a self-registered patient controls the email
    address she signed up with.

    Only patient self-registration uses this — hospital owners and staff are
    identity-checked a stronger way (a human reviewer calling the licence
    number; a set-password link only the invited address can open). See
    ``PatientRegisterView`` and ``LoginView``'s own docstrings for how this
    gate fits into each.

    ``code_hash`` is never the plaintext code — hashed with the same
    machinery as a real password (``make_password``/``check_password``),
    the same discipline this codebase already applies to real passwords.
    A six-digit code is naturally low-entropy even hashed; ``max_attempts``
    and a short ``expires_at`` window are what actually make brute-forcing
    impractical, backed by the same ``auth_sensitive`` request throttle
    (5/min) already applied to every other credential-adjacent endpoint.

    At most one *live* (unconsumed, unexpired) code exists per user at a
    time — requesting a new one (register-again-while-unverified, or an
    explicit resend) invalidates any earlier code for that user first, so
    "which code is current" is never ambiguous.
    """

    MAX_ATTEMPTS = 5
    LIFETIME_MINUTES = 15

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="email_verification_codes",
    )
    code_hash = models.CharField(max_length=128)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        indexes = [models.Index(fields=["user", "consumed_at"])]

    @classmethod
    def issue(cls, user) -> tuple["EmailVerificationCode", str]:  # noqa: UP037 — no `from __future__ import annotations` in this file; unquoting raises NameError, the class isn't bound to its name yet while this signature evaluates
        """Invalidate this user's previous live code (if any) and issue a
        fresh one. Returns the row and the plaintext code — the only place
        the plaintext ever exists outside the email itself; nothing else
        may read it back, since only the hash is stored."""
        import secrets

        cls.objects.filter(user=user, consumed_at__isnull=True).update(consumed_at=timezone.now())
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        row = cls.objects.create(
            user=user,
            code_hash=make_password(code),
            expires_at=timezone.now() + timedelta(minutes=cls.LIFETIME_MINUTES),
        )
        return row, code

    @classmethod
    def verify(cls, user, submitted_code: str) -> bool:
        """Check ``submitted_code`` against this user's current live code.

        A wrong guess still counts against ``attempts`` even though nothing
        is consumed — otherwise the attempt cap could be bypassed by simply
        never letting a guess "count". Once ``MAX_ATTEMPTS`` is reached the
        code is exhausted regardless of correctness, and she must request a
        resend (a fresh code, a fresh attempt budget).
        """
        row = cls.objects.filter(user=user, consumed_at__isnull=True).order_by("-created_at").first()
        if row is None or row.expires_at < timezone.now() or row.attempts >= cls.MAX_ATTEMPTS:
            return False

        row.attempts += 1
        correct = check_password(submitted_code, row.code_hash)
        if correct or row.attempts >= cls.MAX_ATTEMPTS:
            row.consumed_at = timezone.now()
        row.save(update_fields=["attempts", "consumed_at"])
        return correct
