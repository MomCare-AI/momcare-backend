"""Transactional service functions for patient onboarding."""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from momcare_platform.core.locations.services import ensure_default_location
from momcare_platform.core.patients.models import Patient, Pregnancy


class OnboardingError(Exception):
    """Raised when a patient cannot be onboarded; message is safe to show a user."""


@transaction.atomic
def onboard_patient(
    *,
    organization,
    patient_data: dict,
    pregnancy_data: dict | None = None,
) -> Patient:
    """Create a patient and, optionally, her current pregnancy.

    One transaction, so a half-written record is never left behind.

    The location comes from the hospital, never from the request, so onboarding
    cannot place a patient inside another tenant.
    """
    location = ensure_default_location(organization)

    # Both stored as NULL when absent, never "" — two patients missing the
    # same optional-but-unique identifier must not collide with each other
    # under its unique constraint.
    cnic = patient_data.get("cnic") or None
    mrn = patient_data.get("mrn") or None

    try:
        patient = Patient.objects.create(
            location=location,
            organization=organization,
            **{**patient_data, "cnic": cnic, "mrn": mrn},
        )
    except IntegrityError as exc:
        # The serializer already checks both of these and returns a proper
        # field error; reaching here means a concurrent request won the race
        # between that check and this write. Surfaced as a clean 400 rather
        # than a 500.
        if "unique_cnic_per_organization" in str(exc):
            raise OnboardingError(
                "A patient with this CNIC is already registered at this hospital.",
            ) from None
        if "mrn" in str(exc):
            raise OnboardingError("A patient with this MRN already exists.") from None
        raise

    if pregnancy_data:
        create_pregnancy(patient=patient, data=pregnancy_data)

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
    return patient


@transaction.atomic
def reactivate_patient(patient: Patient) -> Patient:
    """Undo a deactivation."""
    patient.reactivate()
    return patient
