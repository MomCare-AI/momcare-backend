"""Transactional service functions for patient enrolment."""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from momcare_platform.core.locations.services import ensure_default_location
from momcare_platform.core.patients.models import Consent, Patient, Pregnancy, PregnancyRiskFactors

MRN_MAX_ATTEMPTS = 5


def organization_prefix(organization) -> str:
    """Readable hospital code, matching the staff employee-id convention."""
    return "".join(ch for ch in organization.name.upper() if ch.isalnum())[:4] or "ORG"


def _candidate_mrn(organization, offset: int = 0) -> str:
    seq = Patient.objects.filter(location__organization=organization).count() + 1 + offset
    return f"{organization_prefix(organization)}-{seq:06d}"


class EnrolmentError(Exception):
    """Raised when a patient cannot be enrolled; message is safe to show a user."""


@transaction.atomic
def enrol_patient(
    *,
    organization,
    recorded_by,
    patient_data: dict,
    pregnancy_data: dict | None = None,
    risk_factor_data: dict | None = None,
    consent: dict | None = None,
) -> Patient:
    """Create a patient, optionally her current pregnancy, and record consent.

    One transaction: a patient stored without the consent that authorised
    storing her would be a record nobody agreed to.

    The location comes from the hospital, never from the request, so enrolment
    cannot place a patient inside another tenant.
    """
    location = ensure_default_location(organization)

    patient = None
    for attempt in range(MRN_MAX_ATTEMPTS):
        # A count()-based sequence races under concurrent enrolment. The unique
        # constraint is the real guard, so a collision retries rather than
        # silently issuing a duplicate identifier.
        try:
            with transaction.atomic():
                patient = Patient.objects.create(
                    location=location,
                    mrn=_candidate_mrn(organization, offset=attempt),
                    **patient_data,
                )
            break
        except IntegrityError:
            if attempt == MRN_MAX_ATTEMPTS - 1:
                raise EnrolmentError(
                    "Could not allocate a medical record number. Please try again.",
                ) from None

    if consent:
        Consent.objects.create(
            patient=patient,
            status=consent.get("status", Consent.STATUS_GRANTED),
            version=consent.get("version", "v1.0"),
            method=consent.get("method", Consent.METHOD_IN_PERSON),
            note=consent.get("note", ""),
            recorded_by=recorded_by,
        )

    if pregnancy_data:
        create_pregnancy(patient=patient, data=pregnancy_data, risk_factor_data=risk_factor_data)

    return patient


@transaction.atomic
def create_pregnancy(*, patient: Patient, data: dict, risk_factor_data: dict | None = None) -> Pregnancy:
    """Open a pregnancy episode, with its risk-factor record.

    Risk factors are always created, even when nothing is known: an absent row
    is indistinguishable from "all negative", while a row of UNKNOWN answers
    tells a clinician exactly what was never asked.
    """
    if patient.pregnancies.filter(status=Pregnancy.STATUS_ACTIVE).exists():
        raise EnrolmentError("This patient already has an active pregnancy.")

    pregnancy = Pregnancy.objects.create(patient=patient, **data)

    if pregnancy.edd and pregnancy.edd_source != Pregnancy.EDD_FROM_LMP:
        pregnancy.edd_confirmed_at = timezone.now()
        pregnancy.save(update_fields=["edd_confirmed_at", "updated_at"])

    PregnancyRiskFactors.objects.create(pregnancy=pregnancy, **(risk_factor_data or {}))
    return pregnancy
