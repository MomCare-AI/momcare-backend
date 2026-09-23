from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from momcare_platform.core.common.models import Deactivatable, TimeStampedModel, UUIDPrimaryKeyModel
from momcare_platform.core.common.obstetrics import calculate_gestational_age, edd_from_lmp

BLOOD_GROUP_CHOICES = [
    ("A+", "A+"),
    ("A-", "A−"),
    ("B+", "B+"),
    ("B-", "B−"),
    ("AB+", "AB+"),
    ("AB-", "AB−"),
    ("O+", "O+"),
    ("O-", "O−"),
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
    # Denormalized from location.organization, the same way Device carries its
    # own direct organization FK — needed so CNIC uniqueness (below) can be
    # scoped correctly. A hospital can have several locations; a
    # location-scoped constraint would miss a duplicate CNIC at a different
    # branch of the same hospital.
    organization = models.ForeignKey(
        "organization.Organization",
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
    # Not unique across the whole platform — the same woman may legitimately be
    # registered at two hospitals. It IS unique within one hospital (see the
    # constraint below): a CNIC is a personal government ID, so two patients at
    # one hospital sharing one is far more likely a data-entry mistake than a
    # real case. null=True, never "", so two CNIC-less patients at the same
    # hospital don't false-positive collide under that constraint.
    # noqa DJ001: null=True on a CharField is exactly what's wanted here. Ruff
    # exempts unique=True fields (see mrn below) because NULL is how you avoid
    # blank-value collisions; this field's uniqueness is a Meta constraint
    # instead, which the rule can't see, so the exemption is stated by hand.
    cnic = models.CharField(_("CNIC"), max_length=20, blank=True, null=True, db_index=True)  # noqa: DJ001
    blood_group = models.CharField(max_length=3, choices=BLOOD_GROUP_CHOICES, blank=True)

    # ── Emergency contact ────────────────────────────────────────────────────
    emergency_contact_name = models.CharField(max_length=100, blank=True)
    emergency_contact_phone = models.CharField(max_length=20, blank=True)
    emergency_contact_relation = models.CharField(max_length=50, blank=True)
    # Captured now for a future notification feature — no send-logic exists
    # yet. Not a new kind of system user; just better-captured data on the
    # existing emergency contact.
    emergency_contact_email = models.EmailField(blank=True, default="")

    # Medical record number — an identifier, not clinical content. Supplied by
    # the hospital from its own numbering, never generated here.
    mrn = models.CharField(max_length=100, unique=True, null=True, blank=True)

    # When she agreed to be monitored. A single date, matching the reference
    # platform's own shape — deliberately not an event history: this records
    # that consent was given, not every time it changed.
    consent_date = models.DateField(null=True, blank=True)

    # The outside clinician involved in her care — the doctor who referred her
    # in, or a specialist she also sees. SET_NULL, not PROTECT: removing a
    # referral contact from the hospital's list must never be blocked by, or
    # cascade into, a patient's record.
    #
    # Distinct from emergency_contact_* above: that is her own family, stored
    # on her row; this is a shared record many patients can point at.
    secondary_provider = models.ForeignKey(
        "staff.SecondaryProvider",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="patients",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["mrn"]),
            models.Index(fields=["phone"]),
            models.Index(fields=["cnic"]),
            models.Index(fields=["last_name", "first_name"]),
        ]
        constraints = [
            # Scoped to the hospital, not the platform: the same woman may
            # legitimately hold a record at two hospitals. Conditional on
            # cnic IS NOT NULL so CNIC-less patients never collide.
            models.UniqueConstraint(
                fields=["organization", "cnic"],
                condition=models.Q(cnic__isnull=False),
                name="unique_cnic_per_organization",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} ({self.mrn})" if self.mrn else self.full_name

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def has_app_account(self) -> bool:
        return self.user_id is not None

    @property
    def current_pregnancy(self):
        """The active pregnancy, if any. A patient has at most one at a time."""
        return self.pregnancies.filter(status=Pregnancy.STATUS_ACTIVE).first()

    @property
    def has_consent(self) -> bool:
        return self.consent_date is not None


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

    # The care team — one of each at a time. The lead clinician (provider) is
    # the accountable name alert escalation routes to; nurse and care_manager
    # are the supporting roles. Deliberately three plain columns rather than a
    # join table: no multi-nurse rotation, no handoff history, matching the
    # reference implementation's shape. See the patient-onboarding design doc.
    #
    # PROTECT preserves who was responsible. Staff is soft-deleted, so this
    # never blocks anything in practice — it guarantees history survives.
    provider = models.ForeignKey(
        "staff.Staff",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="assigned_pregnancies",
    )
    nurse = models.ForeignKey(
        "staff.Staff",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="nursed_pregnancies",
    )
    care_manager = models.ForeignKey(
        "staff.Staff",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="care_managed_pregnancies",
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE, db_index=True)
    outcome_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    # ── Obstetric history ────────────────────────────────────────────────────
    # The standard antenatal booking-form questions, held here on the pregnancy
    # rather than in a table of their own: they are answered once per pregnancy
    # and never independently of it.
    #
    # Three-state rather than boolean, because "not asked" is clinically
    # different from "no" — a boolean silently turns an unknown into a
    # negative, which is exactly the direction that hides risk.
    #
    # Note the trained model does NOT currently read these (it takes 9 vitals;
    # see momcare_model/config.py::FEATURE_COLS). They are recorded because a
    # previous C-section or preeclampsia is textbook obstetric risk, and
    # whether they should feed scoring is one of the questions the outstanding
    # obstetrician review has to answer.
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

    previous_c_section = models.CharField(max_length=10, choices=ANSWER_CHOICES, default=UNKNOWN)
    previous_preeclampsia = models.CharField(max_length=10, choices=ANSWER_CHOICES, default=UNKNOWN)
    previous_gestational_diabetes = models.CharField(max_length=10, choices=ANSWER_CHOICES, default=UNKNOWN)
    previous_preterm_birth = models.CharField(max_length=10, choices=ANSWER_CHOICES, default=UNKNOWN)
    chronic_hypertension = models.CharField(max_length=10, choices=ANSWER_CHOICES, default=UNKNOWN)
    diabetes = models.CharField(max_length=10, choices=ANSWER_CHOICES, default=UNKNOWN)
    multiple_pregnancy = models.CharField(max_length=10, choices=ANSWER_CHOICES, default=UNKNOWN)

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
        return self.provider is not None and self.provider.is_active

    @property
    def present_factors(self) -> list[str]:
        """Only those answered YES — never counts UNKNOWN as present."""
        return [f for f in self.FACTOR_FIELDS if getattr(self, f) == self.YES]

    @property
    def unanswered_factors(self) -> list[str]:
        """Surfaced so a clinician can see what was never asked."""
        return [f for f in self.FACTOR_FIELDS if getattr(self, f) == self.UNKNOWN]


class PatientJoinRequest(UUIDPrimaryKeyModel, TimeStampedModel):
    """A self-registered woman asking a hospital to take her on.

    She registers in the mobile app with no hospital attached, fills in what
    she knows about herself, and sends this to a hospital she picks. Until a
    hospital approves it there is no ``Patient`` row at all — only this
    request — because a Patient's whole invariant is "belongs to exactly one
    hospital", and a record belonging to nobody would break every query that
    relies on it.

    She may have one open request with each of several hospitals at once —
    the constraint below is per hospital, not overall. The first to approve
    gets her: ``Patient.user`` is a one-to-one, so one login belongs to one
    clinical record, and the remaining requests are marked WITHDRAWN rather
    than left pending against a woman who is already somebody's patient.

    ``draft`` holds what she reported, as the same JSON shape the hospital-side
    onboarding endpoint accepts. Deliberately JSON rather than twenty mirrored
    columns: it is transient, it is re-validated by the real serializer before
    anything is created, and duplicating the patient schema here would mean
    changing two places every time a field moves.

    Approval does not create anything itself — it calls the same
    ``onboard_patient()`` a walk-in uses. One creation path, so a woman who
    self-registered and one who walked in are the same kind of record.
    """

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    # Closed because she joined somewhere else, not because this hospital
    # said no. Kept distinct from "rejected" so the record does not blame a
    # hospital for a decision it never made.
    STATUS_WITHDRAWN = "withdrawn"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_WITHDRAWN, "Withdrawn"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="join_requests",
        help_text="Her self-registered account, which has no organization yet.",
    )
    organization = models.ForeignKey(
        "organization.Organization",
        on_delete=models.CASCADE,
        related_name="join_requests",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    draft = models.JSONField(default=dict, blank=True)

    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    decision_note = models.TextField(blank=True)
    # The record approval created. PROTECT, like every other pointer at a
    # clinical record: the request is the audit trail of how that patient
    # came to exist.
    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="join_request",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "status"])]
        constraints = [
            # One live request per hospital at a time. Without this she could
            # spam one hospital, and a reviewer would not know which row is
            # the real one. A decided request never blocks a fresh attempt.
            models.UniqueConstraint(
                fields=["user", "organization"],
                condition=models.Q(status="pending"),
                name="one_pending_join_request_per_hospital",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user.email} → {self.organization.name} ({self.status})"

    @property
    def is_pending(self) -> bool:
        return self.status == self.STATUS_PENDING
