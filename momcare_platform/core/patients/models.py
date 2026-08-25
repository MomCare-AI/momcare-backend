from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from momcare_platform.core.common.models import Deactivatable, TimeStampedModel, UUIDPrimaryKeyModel
from momcare_platform.core.common.obstetrics import calculate_gestational_age, edd_from_lmp

BLOOD_GROUP_CHOICES = [
    ("A+", "A+"), ("A-", "A−"),
    ("B+", "B+"), ("B-", "B−"),
    ("AB+", "AB+"), ("AB-", "AB−"),
    ("O+", "O+"), ("O-", "O−"),
]


class Patient(UUIDPrimaryKeyModel, Deactivatable, TimeStampedModel):
    """A person receiving care — the clinical identity.

    Deliberately separate from ``User``: a clinical identity is not an
    application identity. A woman enrolled at a rural clinic may never have an
    email address or a phone she controls, and she must still have a complete
    record. ``user`` is therefore optional and only appears once she is given
    access to the mobile app.

    Patient owns name, date of birth, gender, phone, CNIC and blood group.
    Where a ``user`` also exists, its own name fields are for authentication
    display only and are never read as clinical truth — one authoritative
    source, so the two can never disagree about who a patient is.

    Belongs to a Location, never directly to the Organization, preserving the
    Organization -> Location -> Patient hierarchy that tenant scoping relies on.
    """

    location = models.ForeignKey(
        "locations.Location",
        on_delete=models.PROTECT,
        related_name="patients",
    )
    # Optional, and SET_NULL: losing an app account must never destroy a
    # clinical record. The previous CASCADE would have deleted the patient.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="patient_profile",
        null=True,
        blank=True,
        help_text="Mobile app account, if she has one. A patient can exist without it.",
    )

    # ── Clinical identity ────────────────────────────────────────────────────
    # default="" satisfies the DB for a non-null column; the API requires a real
    # value, so an unnamed patient can never be created through it.
    first_name = models.CharField(_("first name"), max_length=50, default="")
    last_name = models.CharField(_("last name"), max_length=50, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=20, blank=True)
    # Indexed but NOT unique, unlike User.phone: households share a phone, and a
    # husband's or neighbour's number is often the only contact available.
    phone = models.CharField(_("phone"), max_length=20, blank=True, db_index=True)
    # Also not unique — typos happen, the same woman may be registered at two
    # hospitals, and not everyone holds a CNIC. Duplicates are surfaced for a
    # human to judge rather than blocked by the database.
    cnic = models.CharField(_("CNIC"), max_length=20, blank=True, db_index=True)
    blood_group = models.CharField(max_length=3, choices=BLOOD_GROUP_CHOICES, blank=True)

    # ── Emergency contact ────────────────────────────────────────────────────
    emergency_contact_name = models.CharField(max_length=100, blank=True)
    emergency_contact_phone = models.CharField(max_length=20, blank=True)
    emergency_contact_relation = models.CharField(max_length=50, blank=True)

    # Medical record number — an identifier, not clinical content. Globally
    # unique, but hospital-prefixed so it reads as hospital-scoped.
    mrn = models.CharField(max_length=100, unique=True, null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["mrn"]),
            models.Index(fields=["phone"]),
            models.Index(fields=["cnic"]),
            models.Index(fields=["last_name", "first_name"]),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} ({self.mrn})" if self.mrn else self.full_name

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def organization(self):
        """The hospital, reached through the location — Patient has no direct FK."""
        return self.location.organization

    @property
    def has_app_account(self) -> bool:
        return self.user_id is not None

    @property
    def current_pregnancy(self):
        """The active pregnancy, if any. A patient has at most one at a time."""
        return self.pregnancies.filter(status=Pregnancy.STATUS_ACTIVE).first()

    @property
    def latest_consent(self):
        return self.consents.first()


class Pregnancy(UUIDPrimaryKeyModel, TimeStampedModel):
    """One pregnancy episode.

    Separate from Patient because a woman has several over her life, and each
    one's history is what makes the next one predictable — a previous
    complication is among the strongest risk signals there is. Holding these
    fields on Patient would mean each new pregnancy erased the last.

    Deliberately **not** Deactivatable and never deletable: a pregnancy is
    historical clinical fact. A mistake is corrected, not removed — including
    a loss, which must remain in the record precisely because it matters
    clinically.
    """

    STATUS_ACTIVE = "active"
    STATUS_DELIVERED = "delivered"
    STATUS_MISCARRIAGE = "miscarriage"
    STATUS_TERMINATION = "termination"
    STATUS_STILLBIRTH = "stillbirth"
    STATUS_ENDED_OTHER = "ended_other"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_DELIVERED, "Delivered"),
        (STATUS_MISCARRIAGE, "Miscarriage"),
        (STATUS_TERMINATION, "Termination"),
        (STATUS_STILLBIRTH, "Stillbirth"),
        (STATUS_ENDED_OTHER, "Ended — other"),
    ]

    # How the due date was arrived at. First-trimester ultrasound is more
    # accurate than LMP and supersedes it, so the model records which method is
    # authoritative rather than leaving a bare date of unknown provenance.
    EDD_FROM_LMP = "lmp"
    EDD_FROM_ULTRASOUND = "ultrasound"
    EDD_FROM_CLINICAL = "clinical"
    EDD_SOURCE_CHOICES = [
        (EDD_FROM_LMP, "Last menstrual period"),
        (EDD_FROM_ULTRASOUND, "Ultrasound dating"),
        (EDD_FROM_CLINICAL, "Clinical assessment"),
    ]

    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        related_name="pregnancies",
    )

    lmp = models.DateField(
        _("last menstrual period"),
        null=True,
        blank=True,
        help_text="First day of the last period. May be unknown or unreliable.",
    )
    edd = models.DateField(
        _("estimated delivery date"),
        null=True,
        blank=True,
        help_text="Defaults to LMP + 280 days; override when ultrasound dating differs.",
    )
    edd_source = models.CharField(
        max_length=20,
        choices=EDD_SOURCE_CHOICES,
        default=EDD_FROM_LMP,
    )
    edd_confirmed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the dating was last confirmed or corrected.",
    )

    # Gravida = pregnancies including this one. Para = those reaching viable
    # gestation. Documented here because bare integers invite different
    # readings, and the difference between them is itself a risk signal.
    gravida = models.PositiveSmallIntegerField(null=True, blank=True)
    para = models.PositiveSmallIntegerField(null=True, blank=True)

    # The lead clinician — one accountable name for this pregnancy. This field
    # is permanent, not a placeholder: a future PregnancyCareTeam will add
    # supporting members *alongside* it rather than replacing it, so that
    # change stays additive and needs no migration of live clinical records.
    #
    # PROTECT preserves who was responsible. Staff is soft-deleted, so this
    # never blocks anything in practice — it guarantees history survives.
    assigned_staff = models.ForeignKey(
        "staff.Staff",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="assigned_pregnancies",
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE, db_index=True)
    outcome_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "pregnancies"
        indexes = [
            models.Index(fields=["patient", "status"]),
            models.Index(fields=["edd"]),
        ]
        constraints = [
            # At most one active pregnancy per patient — two would make
            # "which pregnancy is this reading for?" unanswerable.
            models.UniqueConstraint(
                fields=["patient"],
                condition=models.Q(status="active"),
                name="one_active_pregnancy_per_patient",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.patient.full_name} — {self.get_status_display()}"

    def save(self, *args, **kwargs):
        # Derive the EDD when it wasn't given but the LMP was; an explicit EDD
        # always wins, since it usually came from a scan.
        if self.edd is None and self.lmp is not None:
            self.edd = edd_from_lmp(self.lmp)
            self.edd_source = self.EDD_FROM_LMP
        super().save(*args, **kwargs)

    @property
    def region(self) -> str | None:
        """The model region for this pregnancy, via the hospital that enrolled her.

        Reached through the patient's location, the same path every other
        tenant-owned lookup takes. None means the population is outside what the
        model was trained on.
        """
        return self.patient.organization.region

    @property
    def gestational_age(self):
        """Live from the EDD — never stored, so it can never go stale."""
        return calculate_gestational_age(self.edd)

    @property
    def gestational_age_display(self) -> str:
        age = self.gestational_age
        return str(age) if age else "Unknown"

    @property
    def is_active(self) -> bool:
        return self.status == self.STATUS_ACTIVE

    @property
    def has_responsible_clinician(self) -> bool:
        """Whether someone is actually accountable for this pregnancy.

        A clinician who has left the hospital is soft-deleted, so the FK still
        resolves and the record still *looks* assigned. For a system that will
        route alerts to this person, an inactive assignment is the same silent
        failure as no assignment at all, and both must surface.
        """
        return self.assigned_staff is not None and self.assigned_staff.is_active


class PregnancyRiskFactors(UUIDPrimaryKeyModel, TimeStampedModel):
    """Standard obstetric history for one pregnancy.

    Three-state rather than boolean, because "not asked" is clinically
    different from "no". A boolean silently converts an unknown into a
    negative, which is exactly the direction that hides risk.

    These are the factors on any antenatal booking form, not a guess at what a
    particular model will want — extend the list as clinical needs emerge.
    """

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"
    ANSWER_CHOICES = [(YES, "Yes"), (NO, "No"), (UNKNOWN, "Unknown")]

    FACTOR_FIELDS = [
        "previous_c_section",
        "previous_preeclampsia",
        "previous_gestational_diabetes",
        "previous_preterm_birth",
        "chronic_hypertension",
        "diabetes",
        "multiple_pregnancy",
    ]

    pregnancy = models.OneToOneField(
        "patients.Pregnancy",
        on_delete=models.CASCADE,
        related_name="risk_factors",
    )

    previous_c_section = models.CharField(max_length=10, choices=ANSWER_CHOICES, default=UNKNOWN)
    previous_preeclampsia = models.CharField(max_length=10, choices=ANSWER_CHOICES, default=UNKNOWN)
    previous_gestational_diabetes = models.CharField(max_length=10, choices=ANSWER_CHOICES, default=UNKNOWN)
    previous_preterm_birth = models.CharField(max_length=10, choices=ANSWER_CHOICES, default=UNKNOWN)
    chronic_hypertension = models.CharField(max_length=10, choices=ANSWER_CHOICES, default=UNKNOWN)
    diabetes = models.CharField(max_length=10, choices=ANSWER_CHOICES, default=UNKNOWN)
    multiple_pregnancy = models.CharField(max_length=10, choices=ANSWER_CHOICES, default=UNKNOWN)

    class Meta:
        verbose_name = "pregnancy risk factors"
        verbose_name_plural = "pregnancy risk factors"

    def __str__(self) -> str:
        return f"Risk factors — {self.pregnancy.patient.full_name}"

    @property
    def present_factors(self) -> list[str]:
        """Only those answered YES — never counts UNKNOWN as present."""
        return [f for f in self.FACTOR_FIELDS if getattr(self, f) == self.YES]

    @property
    def unanswered_factors(self) -> list[str]:
        """Surfaced so a clinician can see what was never asked."""
        return [f for f in self.FACTOR_FIELDS if getattr(self, f) == self.UNKNOWN]


class Consent(UUIDPrimaryKeyModel, TimeStampedModel):
    """One consent event for one patient.

    An append-only history rather than a field on Patient: consent can be
    withdrawn and given again, policies get new versions, and the question that
    matters later is "what was agreed, when, and who recorded it" — which a
    single overwritten date cannot answer.

    Rows are never modified or deleted; a change of mind is a new row.
    """

    STATUS_GRANTED = "granted"
    STATUS_WITHDRAWN = "withdrawn"
    STATUS_CHOICES = [(STATUS_GRANTED, "Granted"), (STATUS_WITHDRAWN, "Withdrawn")]

    METHOD_IN_PERSON = "in_person"
    METHOD_VERBAL = "verbal"
    METHOD_DIGITAL = "digital"
    METHOD_CHOICES = [
        (METHOD_IN_PERSON, "In person, signed"),
        (METHOD_VERBAL, "Verbal, witnessed"),
        (METHOD_DIGITAL, "Digital"),
    ]

    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        related_name="consents",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    # When this event happened — a withdrawal has no "granted at".
    recorded_at = models.DateTimeField(auto_now_add=True, db_index=True)
    version = models.CharField(
        max_length=20,
        default="v1.0",
        help_text="Which consent policy the patient agreed to.",
    )
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default=METHOD_IN_PERSON)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recorded_consents",
    )
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-recorded_at"]
        verbose_name_plural = "consents"
        indexes = [models.Index(fields=["patient", "-recorded_at"])]

    def __str__(self) -> str:
        return f"{self.patient.full_name} — {self.get_status_display()} ({self.version})"

    @property
    def is_current_grant(self) -> bool:
        return self.status == self.STATUS_GRANTED
