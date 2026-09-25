"""Model-level behavior for StatusLabel and PatientStatus."""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from momcare_platform.core.monitoring.models import PatientStatus, StatusLabel
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db


@pytest.fixture
def hospital(make_hospital):
    return make_hospital("Status Model Hospital")


@pytest.fixture
def patient(hospital):
    return onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
        pregnancy_data={"lmp": timezone.now().date()},
    )


# ── StatusLabel ───────────────────────────────────────────────────────────


def test_status_label_check_constraint_rejects_neither_scope(make_hospital):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            StatusLabel.objects.create(name="No Scope")


def test_status_label_check_constraint_rejects_both_scopes(make_hospital):
    hospital = make_hospital("Reject Both Hospital")
    from momcare_platform.core.locations.models import Location

    branch = Location.objects.create(organization=hospital.org, name="Branch", location_manager=hospital.admin)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            StatusLabel.objects.create(name="Both", organization=hospital.org, location=branch)


def test_duplicate_status_label_name_in_same_org_is_rejected(make_hospital):
    hospital = make_hospital("Duplicate Label Hospital")
    StatusLabel.objects.create(name="Critical", organization=hospital.org)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            StatusLabel.objects.create(name="Critical", organization=hospital.org)


def test_same_status_label_name_allowed_in_different_orgs(make_hospital):
    alpha = make_hospital("Alpha Label Hospital")
    beta = make_hospital("Beta Label Hospital")
    StatusLabel.objects.create(name="Critical", organization=alpha.org)
    # Must not raise -- different organization, same name.
    StatusLabel.objects.create(name="Critical", organization=beta.org)


# ── PatientStatus ─────────────────────────────────────────────────────────


def test_patient_status_blank_name_is_rejected(patient, hospital):
    entry = PatientStatus(patient=patient, name="   ", description="x", color="#ff0000", added_by=hospital.admin)
    with pytest.raises(ValidationError):
        entry.save()


def test_patient_status_invalid_hex_color_is_rejected(patient, hospital):
    entry = PatientStatus(
        patient=patient, name="Stable", description="x", color="not-a-color", added_by=hospital.admin
    )
    with pytest.raises(ValidationError):
        entry.full_clean()


def test_same_name_can_be_logged_twice_for_same_patient(patient, hospital):
    """Deliberate divergence from Neuro_RPM -- see the design doc's Decision 2."""
    PatientStatus.objects.create(
        patient=patient, name="Stable", description="ok", color="#00ff00", added_by=hospital.admin
    )
    # Must not raise -- no uniqueness constraint on (patient, name).
    PatientStatus.objects.create(
        patient=patient, name="Stable", description="ok again", color="#00ff00", added_by=hospital.admin
    )
    assert patient.statuses.filter(name="Stable").count() == 2


def test_current_status_is_the_most_recent_entry(patient, hospital):
    PatientStatus.objects.create(
        patient=patient, name="Waiting", description="d", color="#ffffff", added_by=hospital.admin
    )
    latest = PatientStatus.objects.create(
        patient=patient, name="Stable", description="d", color="#00ff00", added_by=hospital.admin
    )

    assert patient.statuses.first() == latest
