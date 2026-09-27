"""``GET /api/patients/dashboard-kpis`` -- the single combined KPI surface,
matching Neuro_RPM's own ``dashboard-kpis`` shape but with MomCare's own
three workflows/care-activities (no Priority Patients -- billing-cycle
conflict, explicitly declined; no Manage Care Plans -- no CCM concept).
"""

from datetime import timedelta

import pytest
from django.apps import apps as django_apps
from django.conf import settings
from django.utils import timezone

from momcare_platform.core.patients.models import PatientJoinRequest
from momcare_platform.core.patients.services import onboard_patient
from momcare_platform.core.users.models import Role, User

pytestmark = pytest.mark.django_db

KPIS = "/api/patients/dashboard-kpis/"


def _join_request(hospital, *, email, status=PatientJoinRequest.STATUS_PENDING):
    applicant = User.objects.create_user(
        email=email,
        password="TestPass!2026",
        first_name="Applicant",
        last_name="Bibi",
        role=Role.objects.get(code=settings.ROLE_PATIENT),
        is_email_verified=True,
    )
    return PatientJoinRequest.objects.create(user=applicant, organization=hospital.org, status=status)


# Resolved via the app registry, not a static import: RiskAssessment lives in
# modules.pregnancy.vitals, which `core` (this test included) must never
# import statically -- the `core must not import modules` contract.
RiskAssessment = django_apps.get_model("monitoring", "RiskAssessment")


@pytest.fixture
def patient_for(make_hospital):
    def _make(hospital, first_name="Ayesha", *, with_pregnancy=True):
        return onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
            pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=20)} if with_pregnancy else None,
        )

    return _make


def test_shape_with_an_empty_roster(client, make_hospital, auth):
    hospital = make_hospital("Empty Dashboard Hospital")

    response = client.get(KPIS, **auth(hospital.admin.email))

    assert response.status_code == 200
    assert response.json() == {
        "total_patients": 0,
        "active_patients": 0,
        "inactive_patients": 0,
        "pending_join_requests": 0,
        "workflow": {"risk_review": 0, "low_confidence": 0},
        "care_activities": {"monitoring_follow_up": 0, "unseen_readings": 0, "reading_reminder": 0},
    }


def test_total_is_active_plus_inactive(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Total Split Hospital")
    patient_for(hospital, "StillActive")
    inactive = patient_for(hospital, "NoLongerActive")
    inactive.deactivate(by=hospital.admin, reason="Left")

    body = client.get(KPIS, **auth(hospital.admin.email)).json()

    assert body["active_patients"] == 1
    assert body["inactive_patients"] == 1
    assert body["total_patients"] == 2


def test_no_priority_patients_or_manage_careplans_keys(client, make_hospital, auth):
    """Both explicitly declined -- Priority Patients for its billing-cycle
    conflict, Manage Care Plans for having no MomCare CCM equivalent."""
    hospital = make_hospital("No Priority Hospital")

    body = client.get(KPIS, **auth(hospital.admin.email)).json()

    assert "priority_list" not in body["workflow"]
    assert "manage_careplans" not in body["workflow"]
    assert "out_of_range" not in body["care_activities"]


def test_risk_review_count_reflects_a_pending_high_assessment(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Risk Review KPI Hospital")
    patient = patient_for(hospital)
    pregnancy = patient.current_pregnancy
    RiskAssessment.objects.create(
        pregnancy=pregnancy,
        risk_level=RiskAssessment.LEVEL_HIGH,
        final_risk_level=RiskAssessment.LEVEL_HIGH,
        confidence=0.95,
        review_status=RiskAssessment.REVIEW_PENDING,
    )

    body = client.get(KPIS, **auth(hospital.admin.email)).json()

    assert body["workflow"]["risk_review"] == 1
    assert body["workflow"]["low_confidence"] == 0


def test_low_confidence_count_reflects_a_flagged_assessment(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Low Confidence KPI Hospital")
    patient = patient_for(hospital)
    pregnancy = patient.current_pregnancy
    RiskAssessment.objects.create(
        pregnancy=pregnancy,
        risk_level=RiskAssessment.LEVEL_LOW,
        final_risk_level=RiskAssessment.LEVEL_LOW,
        confidence=0.4,
        flagged_for_review=True,
        review_status=RiskAssessment.REVIEW_PENDING,
    )

    body = client.get(KPIS, **auth(hospital.admin.email)).json()

    assert body["workflow"]["low_confidence"] == 1
    assert body["workflow"]["risk_review"] == 0


def test_monitoring_follow_up_count_reflects_a_never_monitored_patient(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Monitoring KPI Hospital")
    patient_for(hospital, with_pregnancy=False)

    body = client.get(KPIS, **auth(hospital.admin.email)).json()

    assert body["care_activities"]["monitoring_follow_up"] == 1


def test_reading_reminder_count_reflects_a_never_read_patient(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Reminder KPI Hospital")
    patient_for(hospital)

    body = client.get(KPIS, **auth(hospital.admin.email)).json()

    assert body["care_activities"]["reading_reminder"] == 1


def test_kpis_are_not_readable_across_hospitals(client, make_hospital, patient_for, auth):
    hospital = make_hospital("KPI Isolation Hospital A")
    rival = make_hospital("KPI Isolation Hospital B")
    patient_for(hospital)
    patient_for(rival)
    patient_for(rival)

    body = client.get(KPIS, **auth(hospital.admin.email)).json()

    assert body["total_patients"] == 1


def test_kpis_respect_the_is_active_filter(client, make_hospital, patient_for, auth):
    hospital = make_hospital("KPI Filter Hospital")
    patient_for(hospital, "StillActive")
    inactive = patient_for(hospital, "NoLongerActive")
    inactive.deactivate(by=hospital.admin, reason="Left")

    body = client.get(f"{KPIS}?is_active=true", **auth(hospital.admin.email)).json()

    assert body["active_patients"] == 1
    assert body["inactive_patients"] == 0


# ── pending_join_requests ───────────────────────────────────────────────


def test_pending_join_requests_counts_only_pending(client, make_hospital, auth):
    hospital = make_hospital("Join Request KPI Hospital")
    _join_request(hospital, email="pending1@joinrequestkpi.test")
    _join_request(hospital, email="pending2@joinrequestkpi.test")
    _join_request(hospital, email="approved@joinrequestkpi.test", status=PatientJoinRequest.STATUS_APPROVED)
    _join_request(hospital, email="rejected@joinrequestkpi.test", status=PatientJoinRequest.STATUS_REJECTED)
    _join_request(hospital, email="withdrawn@joinrequestkpi.test", status=PatientJoinRequest.STATUS_WITHDRAWN)

    body = client.get(KPIS, **auth(hospital.admin.email)).json()

    assert body["pending_join_requests"] == 2


def test_pending_join_requests_is_not_readable_across_hospitals(client, make_hospital, auth):
    hospital = make_hospital("Join Request Isolation Hospital A")
    rival = make_hospital("Join Request Isolation Hospital B")
    _join_request(rival, email="pending@joinrequestisolation.test")

    body = client.get(KPIS, **auth(hospital.admin.email)).json()

    assert body["pending_join_requests"] == 0


def test_pending_join_requests_is_not_affected_by_roster_filters(client, make_hospital, patient_for, auth):
    """A join request has no location/care-team assignment yet -- ?is_active=
    and friends narrow the existing patient roster, not the join-request
    queue, so this count must not change when they're applied."""
    hospital = make_hospital("Join Request Filter Hospital")
    patient_for(hospital)
    _join_request(hospital, email="pending@joinrequestfilter.test")

    body = client.get(f"{KPIS}?is_active=true", **auth(hospital.admin.email)).json()

    assert body["pending_join_requests"] == 1
