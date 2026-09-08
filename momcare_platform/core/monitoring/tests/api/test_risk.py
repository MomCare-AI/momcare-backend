"""Risk assessment through the API — persistence, the attention queue, and the
tenant boundary.

Vitals below are the same known-good combinations verified against the real
trained model in test_reassess_risk.py — this file exercises the API surface
on top of that already-proven scoring behaviour, not the model itself.
"""

import json
from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from momcare_platform.core.alerts.models import Alert
from momcare_platform.core.monitoring.models import RiskAssessment, VitalReading
from momcare_platform.core.monitoring.services import reassess_risk
from momcare_platform.core.patients.models import Consent
from momcare_platform.core.patients.services import enrol_patient

pytestmark = pytest.mark.django_db

ATTENTION = "/api/attention/"

LOW_VITALS = {
    "systolic_bp": 118, "diastolic_bp": 76, "heart_rate": 82, "body_temp_f": 98.2,
    "hemoglobin": 12.1, "blood_glucose": 92, "stress_score": 3, "phys_activity_score": 6,
}
MEDIUM_VITALS = {
    "systolic_bp": 138, "diastolic_bp": 88, "heart_rate": 95, "body_temp_f": 98.6,
    "hemoglobin": 10.5, "blood_glucose": 110, "stress_score": 5, "phys_activity_score": 4,
}
HIGH_VITALS = {
    "systolic_bp": 185, "diastolic_bp": 125, "heart_rate": 130, "body_temp_f": 103.0,
    "hemoglobin": 6.0, "blood_glucose": 250, "stress_score": 9, "phys_activity_score": 1,
}


@pytest.fixture
def pregnancy_for(db):
    def _make(hospital, *, first_name="Ayesha", clinician=None):
        patient = enrol_patient(
            organization=hospital.org,
            recorded_by=hospital.admin,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
            pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=28)},
            consent={"status": Consent.STATUS_GRANTED},
        )
        pregnancy = patient.current_pregnancy
        if clinician is not None:
            pregnancy.assigned_staff = clinician.staff
            pregnancy.save(update_fields=["assigned_staff", "updated_at"])
        return pregnancy

    return _make


def add_reading(pregnancy, vitals, *, minutes_ago=1):
    return VitalReading.objects.create(
        pregnancy=pregnancy,
        recorded_at=timezone.now() - timedelta(minutes=minutes_ago),
        source=VitalReading.SOURCE_MANUAL,
        **vitals,
    )


# ── Assessment is recorded on change only ────────────────────────────────────


def test_a_dangerous_reading_produces_an_assessment(make_hospital, pregnancy_for):
    hospital = make_hospital("Assess Hospital")
    pregnancy = pregnancy_for(hospital)
    add_reading(pregnancy, HIGH_VITALS)

    assessment = reassess_risk(pregnancy)

    assert assessment is not None
    assert assessment.final_risk_level == RiskAssessment.LEVEL_HIGH
    assert assessment.bp_category == "Hypertensive Crisis"


def test_an_unchanged_level_does_not_write_another_row(make_hospital, pregnancy_for):
    """Otherwise a reading every few minutes would bury the transitions that
    actually matter under millions of identical rows."""
    hospital = make_hospital("No Churn Hospital")
    pregnancy = pregnancy_for(hospital)
    add_reading(pregnancy, LOW_VITALS, minutes_ago=10)
    reassess_risk(pregnancy)

    add_reading(pregnancy, LOW_VITALS)
    second = reassess_risk(pregnancy)

    assert second is None
    assert pregnancy.risk_assessments.count() == 1


def test_a_worsening_level_is_recorded_with_its_previous(make_hospital, pregnancy_for):
    hospital = make_hospital("Escalating Hospital")
    pregnancy = pregnancy_for(hospital)
    add_reading(pregnancy, MEDIUM_VITALS, minutes_ago=10)
    reassess_risk(pregnancy)

    add_reading(pregnancy, HIGH_VITALS)
    worsened = reassess_risk(pregnancy)

    assert worsened is not None
    assert worsened.final_risk_level == RiskAssessment.LEVEL_HIGH
    assert worsened.previous_risk_level == RiskAssessment.LEVEL_MEDIUM
    assert pregnancy.risk_assessments.count() == 2


def test_recovery_is_recorded_too(make_hospital, pregnancy_for):
    """Going back to Low is a transition worth keeping — it is how a
    clinician sees that an intervention worked."""
    hospital = make_hospital("Recovery Hospital")
    pregnancy = pregnancy_for(hospital)
    add_reading(pregnancy, HIGH_VITALS, minutes_ago=10)
    reassess_risk(pregnancy)

    add_reading(pregnancy, LOW_VITALS)
    recovered = reassess_risk(pregnancy)

    assert recovered is not None
    assert recovered.final_risk_level == RiskAssessment.LEVEL_LOW
    assert recovered.previous_risk_level == RiskAssessment.LEVEL_HIGH


def test_recording_a_reading_scores_it_immediately(client, make_hospital, pregnancy_for, auth):
    """A dangerous reading is judged as it arrives, not when a scheduler runs."""
    hospital = make_hospital("Immediate Hospital")
    pregnancy = pregnancy_for(hospital)

    response = client.post(
        f"/api/pregnancies/{pregnancy.id}/readings/",
        data=json.dumps({**HIGH_VITALS, "source": "manual"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    assert response.json()["risk_changed"] is True
    assert response.json()["risk_level"] == RiskAssessment.LEVEL_HIGH


# ── The queue ────────────────────────────────────────────────────────────────


def test_the_queue_lists_only_patients_needing_attention(
    client, make_hospital, pregnancy_for, auth,
):
    hospital = make_hospital("Queue Hospital")
    stable = pregnancy_for(hospital, first_name="Stable")
    at_risk = pregnancy_for(hospital, first_name="AtRisk")
    add_reading(stable, LOW_VITALS)
    add_reading(at_risk, HIGH_VITALS)
    reassess_risk(stable)
    reassess_risk(at_risk)

    body = client.get(ATTENTION, **auth(hospital.admin.email)).json()

    assert body["count"] == 1
    assert body["results"][0]["full_name"] == "AtRisk Bibi"
    assert body["results"][0]["risk_level"] == "high"


def test_the_queue_puts_the_most_severe_first(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Ordering Hospital")
    medium = pregnancy_for(hospital, first_name="Medium")
    high = pregnancy_for(hospital, first_name="High")
    add_reading(medium, MEDIUM_VITALS)
    add_reading(high, HIGH_VITALS)
    reassess_risk(medium)
    reassess_risk(high)

    results = client.get(ATTENTION, **auth(hospital.admin.email)).json()["results"]

    assert [r["risk_level"] for r in results] == ["high", "medium"]


def test_the_queue_carries_the_gestational_age_and_the_responsible_clinician(
    client, make_hospital, pregnancy_for, auth,
):
    """The list is scanned, not read — it must say enough to triage without
    opening the record. (Detailed reasons live on the assessment itself,
    fetched separately via /risk/ — the queue row stays deliberately small.)"""
    hospital = make_hospital("Triage Hospital")
    pregnancy = pregnancy_for(hospital)
    add_reading(pregnancy, HIGH_VITALS)
    reassess_risk(pregnancy)

    row = client.get(ATTENTION, **auth(hospital.admin.email)).json()["results"][0]

    assert row["gestational_age"].endswith("d")
    # Nobody was assigned at enrolment, and the queue has to show that: an
    # unattended high-risk patient is the case most likely to be missed.
    assert row["has_responsible_clinician"] is False


def test_the_queue_carries_the_clinical_categories_via_the_risk_endpoint(
    client, make_hospital, pregnancy_for, auth,
):
    """The category detail the old queue row used to carry inline now lives
    on the assessment, one call away — proving it's still reachable."""
    hospital = make_hospital("Detail Hospital")
    pregnancy = pregnancy_for(hospital)
    add_reading(pregnancy, HIGH_VITALS)
    reassess_risk(pregnancy)

    current = client.get(
        f"/api/pregnancies/{pregnancy.id}/risk/",
        **auth(hospital.admin.email),
    ).json()["current"]

    assert current["bp_category"] == "Hypertensive Crisis"
    assert current["hemoglobin_category"] == "Severe Anemia"


def test_verifying_records_who_reviewed_it(
    client, make_hospital, make_staff, pregnancy_for, auth,
):
    """A clinician, because verifying is a claim to have reviewed the case.

    A hospital administrator is not required to have clinical training, and an
    assessment marked reviewed by somebody who could not review it is worse than
    one left unreviewed - the queue would look attended to.
    """
    hospital = make_hospital("Verify Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, email="doctor@verifyrisk.test")
    pregnancy = pregnancy_for(hospital)
    add_reading(pregnancy, HIGH_VITALS)
    assessment = reassess_risk(pregnancy)
    assert assessment is not None
    assert assessment.needs_review is True

    response = client.post(
        f"/api/pregnancies/{pregnancy.id}/risk/{assessment.id}/verify/",
        data=json.dumps({"confirmed_risk_level": "high"}),
        content_type="application/json",
        **auth(doctor.email),
    )

    assert response.status_code == 200
    assessment.refresh_from_db()
    assert assessment.verified_by == doctor
    assert assessment.needs_review is False


def test_a_reviewed_case_sorts_below_an_unreviewed_one_of_equal_severity(
    client, make_hospital, make_staff, pregnancy_for, auth,
):
    hospital = make_hospital("Review Order Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, email="doctor@revieworder.test")
    reviewed = pregnancy_for(hospital, first_name="Reviewed")
    unreviewed = pregnancy_for(hospital, first_name="Unreviewed")
    add_reading(reviewed, HIGH_VITALS)
    add_reading(unreviewed, HIGH_VITALS)
    reviewed_assessment = reassess_risk(reviewed)
    reassess_risk(unreviewed)
    assert reviewed_assessment is not None

    client.post(
        f"/api/pregnancies/{reviewed.id}/risk/{reviewed_assessment.id}/verify/",
        data=json.dumps({"confirmed_risk_level": "high"}),
        content_type="application/json",
        **auth(doctor.email),
    )

    results = client.get(ATTENTION, **auth(hospital.admin.email)).json()["results"]

    assert results[0]["full_name"] == "Unreviewed Bibi"


def test_acknowledging_the_alert_records_who_looked(
    client, make_hospital, make_staff, pregnancy_for, auth,
):
    """Acknowledgement lives on the Alert raised for an actionable assessment,
    not on the assessment itself — a distinct, clinician-only act of stopping
    the escalation clock."""
    hospital = make_hospital("Ack Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, email="doctor@ackrisk.test")
    pregnancy = pregnancy_for(hospital, clinician=doctor)
    add_reading(pregnancy, HIGH_VITALS)
    reassess_risk(pregnancy)
    alert = Alert.objects.get(pregnancy=pregnancy)

    response = client.post(
        f"/api/alerts/{alert.id}/acknowledge/",
        **auth(doctor.email),
    )

    assert response.status_code == 200
    alert.refresh_from_db()
    assert alert.acknowledged_by == doctor
    assert alert.status == Alert.STATUS_ACKNOWLEDGED


# ── Tenant isolation ─────────────────────────────────────────────────────────


def test_the_queue_never_crosses_hospitals(client, make_hospital, pregnancy_for, auth):
    alpha = make_hospital("Alpha Risk")
    beta = make_hospital("Beta Risk")
    beta_pregnancy = pregnancy_for(beta, first_name="BetaPatient")
    add_reading(beta_pregnancy, HIGH_VITALS)
    reassess_risk(beta_pregnancy)

    body = client.get(ATTENTION, **auth(alpha.admin.email)).json()

    assert body["count"] == 0


def test_risk_history_is_not_readable_across_hospitals(
    client, make_hospital, pregnancy_for, auth,
):
    alpha = make_hospital("Alpha History")
    beta = make_hospital("Beta History")
    beta_pregnancy = pregnancy_for(beta)
    add_reading(beta_pregnancy, HIGH_VITALS)
    reassess_risk(beta_pregnancy)

    response = client.get(
        f"/api/pregnancies/{beta_pregnancy.id}/risk/",
        **auth(alpha.admin.email),
    )

    assert response.status_code == 404
