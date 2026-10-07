"""Transactional service functions for patient onboarding."""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from momcare_platform.core.common.rls import bypass_rls
from momcare_platform.core.locations.services import ensure_default_location
from momcare_platform.core.patients.models import Patient, PatientJoinRequest, Pregnancy


class OnboardingError(Exception):
    """Raised when a patient cannot be onboarded; message is safe to show a user."""


@transaction.atomic
def onboard_patient(
    *,
    organization,
    patient_data: dict,
    pregnancy_data: dict | None = None,
    location=None,
) -> Patient:
    """Create a patient and, optionally, her current pregnancy.

    One transaction, so a half-written record is never left behind.

    The location is resolved on the server, never taken from a request body, so
    onboarding cannot place a patient inside another tenant: a ``location`` is
    honoured only when it is an active branch of this very hospital (it comes
    from a join request she sent), and anything else falls back to the
    hospital's default.
    """
    if location is None or location.organization_id != organization.id or not location.is_active:
        location = ensure_default_location(organization)

    # Both stored as NULL when absent, never "" — two patients missing the
    # same optional-but-unique identifier must not collide with each other
    # under its unique constraint.
    national_id = patient_data.get("national_id") or None
    mrn = patient_data.get("mrn") or None

    try:
        patient = Patient.objects.create(
            location=location,
            organization=organization,
            **{**patient_data, "national_id": national_id, "mrn": mrn},
        )
    except IntegrityError as exc:
        # The serializer already checks both of these and returns a proper
        # field error; reaching here means a concurrent request won the race
        # between that check and this write. Surfaced as a clean 400 rather
        # than a 500.
        if "unique_national_id_per_organization" in str(exc):
            raise OnboardingError(
                "A patient with this national ID is already registered at this hospital.",
            ) from None
        if "mrn" in str(exc):
            raise OnboardingError("A patient with this MRN already exists.") from None
        raise

    if pregnancy_data:
        create_pregnancy(patient=patient, data=pregnancy_data)

    # core.ai is another core app -- no import-linter concern reaching it
    # directly, unlike modules.pregnancy.vitals elsewhere in this file.
    # Local import purely to avoid a hard top-level circular dependency
    # between core.patients and core.ai (core.ai's own services module
    # imports Patient from here for its select_for_update() lock).
    #
    # Called here, after the pregnancy above, rather than from a Patient
    # post_save signal: a signal fires before this function creates the
    # pregnancy, so the very first summary would always describe a patient
    # onboarded with obstetric data as having none.
    from momcare_platform.core.ai.services import generate_patient_summary  # noqa: PLC0415

    generate_patient_summary(patient)

    return patient


class AlreadyOnboardedError(OnboardingError):
    """She is already some hospital's patient, so no second record may be made."""


@transaction.atomic
def onboard_from_join_request(
    *,
    join_request: PatientJoinRequest,
    organization,
    patient_data: dict,
    pregnancy_data: dict | None,
    decided_by,
) -> Patient:
    """Saving the onboarding form for a woman who asked to join: the approval.

    One transaction: the Patient (and pregnancy, if any), the link to her login,
    her account joining the hospital, the request closing, and her other open
    requests being withdrawn either all happen or none do.
    """
    applicant = join_request.user

    # Cross-tenant on purpose: the record that already claims her may belong to a
    # DIFFERENT hospital, which a scoped read cannot see. Without bypass_rls this
    # guard would never fire in production -- local and test databases bypass RLS
    # anyway, so no test could show it.
    with bypass_rls():
        if Patient.objects.filter(user=applicant).exists():
            raise AlreadyOnboardedError(
                "This applicant has already been accepted by another hospital and is under their care.",
            )

    patient = onboard_patient(
        organization=organization,
        patient_data=patient_data,
        pregnancy_data=pregnancy_data,
        location=join_request.location,
    )

    # Link her login to the clinical record now that one exists, and make her
    # account part of the hospital: her token's organization is what scopes her
    # to her own care plan and readings. bypass_rls because her User row has no
    # organization yet, so this hospital's scoped session cannot see it.
    with bypass_rls():
        patient.user = applicant
        patient.save(update_fields=["user", "updated_at"])
        applicant.organization = organization
        applicant.save(update_fields=["organization", "updated_at"])

        join_request.status = PatientJoinRequest.STATUS_APPROVED
        join_request.patient = patient
        join_request.decided_by = decided_by
        join_request.decided_at = timezone.now()
        join_request.save(update_fields=["status", "patient", "decided_by", "decided_at", "updated_at"])

        # WITHDRAWN, not REJECTED -- those hospitals never said no, and a record
        # claiming they did would be a lie about a decision nobody made. These
        # rows belong to OTHER hospitals, hence the bypass.
        PatientJoinRequest.objects.filter(
            user=applicant,
            status=PatientJoinRequest.STATUS_PENDING,
        ).exclude(pk=join_request.pk).update(
            status=PatientJoinRequest.STATUS_WITHDRAWN,
            decided_at=timezone.now(),
            decision_note="Withdrawn automatically — she was accepted by another hospital.",
        )
    return patient


@transaction.atomic
def create_pregnancy(*, patient: Patient, data: dict) -> Pregnancy:
    """Open a pregnancy episode.

    The obstetric-history answers live on the pregnancy itself and default to
    "unknown", so a booking form nobody filled in is distinguishable from one
    answered "no" to everything.
    """
    if patient.pregnancies.filter(status=Pregnancy.STATUS_ACTIVE).exists():
        raise OnboardingError("This patient already has an active pregnancy.")

    pregnancy = Pregnancy.objects.create(patient=patient, **data)

    if pregnancy.edd and pregnancy.edd_source != Pregnancy.EDD_FROM_LMP:
        pregnancy.edd_confirmed_at = timezone.now()
        pregnancy.save(update_fields=["edd_confirmed_at", "updated_at"])

    return pregnancy


@transaction.atomic
def deactivate_patient(patient: Patient, *, by=None, reason: str = "") -> Patient:
    """Deactivate a patient — never delete. Clinical records survive."""
    patient.deactivate(by=by, reason=reason)
    # core.ai is another core app -- no import-linter concern reaching it
    # directly, unlike modules.pregnancy.vitals elsewhere in this file.
    # Local import purely to avoid a hard top-level circular dependency
    # between core.patients and core.ai (core.ai's own services module
    # imports Patient from here for its select_for_update() lock).
    from momcare_platform.core.ai.services import generate_patient_summary  # noqa: PLC0415

    generate_patient_summary(patient, deactivated=True)
    return patient


@transaction.atomic
def reactivate_patient(patient: Patient) -> Patient:
    """Undo a deactivation."""
    patient.reactivate()
    return patient
