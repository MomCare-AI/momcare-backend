from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _
from timezone_field import TimeZoneField

from momcare_platform.core.common.languages import SUPPORTED_LANGUAGES
from momcare_platform.core.common.models import AddressMixin, UUIDPrimaryKeyModel
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
    REQUIRED_FIELDS: list[str] = []

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
        return self.role.code if self.role_id else None
