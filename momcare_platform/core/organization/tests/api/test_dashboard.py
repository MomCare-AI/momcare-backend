"""The portal overview: how the ward looks right now, and what has been
happening.

Two questions, both answered from real rows. Neither invents a number - a
pregnancy nobody has assessed is its own category, "not_assessed", rather than
being folded into "stable", because a patient nobody has measured is not a
patient who is well.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from momcare_platform.core.monitoring.models import VitalReading
from momcare_platform.core.monitoring.services import reassess_risk
from momcare_platform.core.organization.models import AuditLog
from momcare_platform.core.patients.models import Consent
from momcare_platform.core.patients.services import enrol_patient

pytestmark = pytest.mark.django_db

SUMMARY = "/api/dashboard/summary/"


@pytest.fixture
def pregnancy_for(db):
    def _make(hospital, first_name="Ayesha"):
        patient = enrol_patient(
            organization=hospital.org,
            recorded_by=hospital.admin,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
            pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=28)},
            consent={"status": Consent.STATUS_GRANTED},
        )
        return patient.current_pregnancy

    return _make


def add_bp(pregnancy, systolic, diastolic):
    VitalReading.objects.create(
        pregnancy=pregnancy,
        reading_type=VitalReading.TYPE_BLOOD_PRESSURE,
        value=systolic,
        value_secondary=diastolic,
        recorded_at=timezone.now(),
        source=VitalReading.SOURCE_MANUAL,
    )


# -- Risk distribution ---------------------------------------------------------


def test_a_patient_never_assessed_is_its_own_category_not_stable(client, make_hospital, auth, pregnancy_for):
    """The whole point of the endpoint. Folding "never measured" into "stable"
    would be the dashboard inventing reassurance nobody earned."""
    hospital = make_hospital("Unmeasured Hospital")
    pregnancy_for(hospital, "Nadia")  # enrolled, never given a reading

    response = client.get(SUMMARY, **auth(hospital.admin.email))

    assert response.status_code == 200, response.content
    risk = response.json()["risk"]
    assert risk["not_assessed"] == 1
    assert risk["stable"] == 0
    assert risk["total"] == 1
    assert risk["needing_attention"] == 0


def test_risk_levels_are_counted_by_the_latest_assessment_only(client, make_hospital, auth, pregnancy_for):
    """Worsening replaces the count, it does not add to it - one pregnancy is
    one row in the distribution, however many readings led to it."""
    hospital = make_hospital("Distribution Hospital")
    critical = pregnancy_for(hospital, "Ayesha")
    add_bp(critical, 168, 112)
    reassess_risk(critical)

    stable = pregnancy_for(hospital, "Hina")
    add_bp(stable, 115, 74)
    reassess_risk(stable)

    response = client.get(SUMMARY, **auth(hospital.admin.email))

    risk = response.json()["risk"]
    assert risk["critical"] == 1
    assert risk["stable"] == 1
    assert risk["total"] == 2
    assert risk["needing_attention"] == 1


def test_a_closed_pregnancy_is_not_counted(client, make_hospital, auth, pregnancy_for):
    """The ward view is about who is currently being watched."""
    from momcare_platform.core.patients.models import Pregnancy  # noqa: PLC0415

    hospital = make_hospital("Closed Hospital")
    pregnancy = pregnancy_for(hospital)
    add_bp(pregnancy, 168, 112)
    reassess_risk(pregnancy)
    pregnancy.status = Pregnancy.STATUS_DELIVERED
    pregnancy.save(update_fields=["status", "updated_at"])

    response = client.get(SUMMARY, **auth(hospital.admin.email))

    assert response.json()["risk"]["total"] == 0


def test_another_hospitals_patients_are_never_counted(client, make_hospital, auth, pregnancy_for):
    ours = make_hospital("Ours Hospital")
    theirs = make_hospital("Theirs Hospital")
    pregnancy_for(theirs)

    response = client.get(SUMMARY, **auth(ours.admin.email))

    assert response.json()["risk"]["total"] == 0


# -- Recent activity -------------------------------------------------------------


def test_recent_activity_reads_the_real_audit_log(client, make_hospital, auth):
    """Not a placeholder feed - the same rows AuditLogMiddleware already writes
    for every PHI-touching request."""
    hospital = make_hospital("Activity Hospital")
    AuditLog.objects.create(
        user=hospital.admin,
        action=AuditLog.ACTION_READ,
        resource="patients",
        resource_id="",
        endpoint="/api/patients/",
    )

    response = client.get(SUMMARY, **auth(hospital.admin.email))

    activity = response.json()["activity"]
    assert len(activity) == 1
    assert activity[0]["action"] == "READ"
    assert activity[0]["resource"] == "patients"
    assert activity[0]["actor"] == hospital.admin.get_full_name()
    assert activity[0]["at"] is not None


def test_activity_is_newest_first_and_capped(client, make_hospital, auth):
    hospital = make_hospital("Busy Hospital")
    for i in range(15):
        AuditLog.objects.create(
            user=hospital.admin,
            action=AuditLog.ACTION_READ,
            resource=f"patients/{i}",
            resource_id="",
            endpoint="/api/patients/",
        )

    response = client.get(SUMMARY, **auth(hospital.admin.email))

    activity = response.json()["activity"]
    assert len(activity) == 12  # ACTIVITY_LIMIT
    assert activity[0]["resource"] == "patients/14"


def test_activity_from_another_hospital_never_appears(client, make_hospital, auth):
    ours = make_hospital("Quiet Hospital")
    theirs = make_hospital("Noisy Hospital")
    AuditLog.objects.create(
        user=theirs.admin,
        action=AuditLog.ACTION_READ,
        resource="patients",
        resource_id="",
        endpoint="/api/patients/",
    )

    response = client.get(SUMMARY, **auth(ours.admin.email))

    assert response.json()["activity"] == []


def test_an_anonymous_audit_entry_has_no_hospital_to_belong_to(client, make_hospital, auth):
    """AuditLog.user is SET_NULL on delete, but users are never hard-deleted
    anywhere in this project - only deactivated. So the only real source of a
    null user is an anonymous request, and an anonymous entry has no
    organization to attribute it to. Excluding it from every hospital's feed
    is correct, not a bug - showing it on someone's dashboard would be a
    guess about whose activity it was."""
    hospital = make_hospital("Departed Hospital")
    AuditLog.objects.create(
        user=None,
        action=AuditLog.ACTION_UPDATE,
        resource="alerts",
        resource_id="",
        endpoint="/api/alerts/1/acknowledge/",
    )

    response = client.get(SUMMARY, **auth(hospital.admin.email))

    assert response.json()["activity"] == []


# -- Access ----------------------------------------------------------------------


def test_an_anonymous_caller_is_refused(client):
    response = client.get(SUMMARY)
    assert response.status_code in (401, 403)


def test_a_patient_account_is_refused(client, make_hospital, django_user_model, auth):
    """A patient has an organization, unlike a platform admin - so only an
    explicit role check keeps them out. They must never see the ward's whole
    risk distribution or staff's PHI-access history."""
    from django.conf import settings  # noqa: PLC0415

    from momcare_platform.core.users.models import Role  # noqa: PLC0415

    hospital = make_hospital("Patient Hospital")
    patient_user = django_user_model.objects.create_user(
        email="patient@momcare.test",
        password="Sup3rSecret!",
        first_name="A",
        last_name="Patient",
        role=Role.objects.get(code=settings.ROLE_PATIENT),
    )
    patient_user.organization = hospital.org
    patient_user.save(update_fields=["organization", "updated_at"])

    response = client.get(SUMMARY, **auth(patient_user.email, password="Sup3rSecret!"))

    assert response.status_code == 403


def test_a_platform_admin_with_no_hospital_gets_a_clear_404(client, django_user_model, auth):
    """MyOrganizationView already draws this line: platform admins work
    through Django admin, not a hospital dashboard."""
    from django.conf import settings  # noqa: PLC0415

    from momcare_platform.core.users.models import Role  # noqa: PLC0415

    admin = django_user_model.objects.create_user(
        email="platform@momcare.test",
        password="Sup3rSecret!",
        first_name="Platform",
        last_name="Admin",
        role=Role.objects.get(code=settings.ROLE_PLATFORM_ADMIN),
    )

    response = client.get(SUMMARY, **auth(admin.email, password="Sup3rSecret!"))

    assert response.status_code == 404
