"""Risk assessment through the API — persistence, the queue, and the tenant boundary."""

import json
from datetime import timedelta

import pytest
from django.utils import timezone

from momcare_platform.core.monitoring.models import RiskAssessment, VitalReading
from momcare_platform.core.monitoring.services import reassess_risk
from momcare_platform.core.patients.models import Consent
from momcare_platform.core.patients.services import enrol_patient

pytestmark = pytest.mark.django_db

ATTENTION = "/api/attention/"


@pytest.fixture
def pregnancy_for(db):
    def _make(hospital, *, first_name="Ayesha"):
        patient = enrol_patient(
            organization=hospital.org,
            recorded_by=hospital.admin,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
            pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=28)},
            consent={"status": Consent.STATUS_GRANTED},
        )
        return patient.current_pregnancy

    return _make


def add_bp(pregnancy, systolic, diastolic, *, minutes_ago=1):
    return VitalReading.objects.create(
        pregnancy=pregnancy,
        reading_type=VitalReading.TYPE_BLOOD_PRESSURE,
        value=systolic,
        value_secondary=diastolic,
        recorded_at=timezone.now() - timedelta(minutes=minutes_ago),
        source=VitalReading.SOURCE_MANUAL,
    )


# ── Assessment is recorded on change only ────────────────────────────────────


def test_a_dangerous_reading_produces_an_assessment(make_hospital, pregnancy_for):
    hospital = make_hospital("Assess Hospital")
    pregnancy = pregnancy_for(hospital)
    add_bp(pregnancy, 168, 112)

    assessment = reassess_risk(pregnancy)

    assert assessment is not None
    assert assessment.level == RiskAssessment.LEVEL_CRITICAL
    assert assessment.source == RiskAssessment.SOURCE_RULES
    assert assessment.engine_version.startswith("rules-")
    assert "preeclampsia" in assessment.reasons[0].lower()


def test_an_unchanged_level_does_not_write_another_row(make_hospital, pregnancy_for):
    """Otherwise a reading every few minutes would bury the transitions that
    actually matter under millions of identical rows."""
    hospital = make_hospital("No Churn Hospital")
    pregnancy = pregnancy_for(hospital)
    add_bp(pregnancy, 150, 95)
    reassess_risk(pregnancy)

    add_bp(pregnancy, 152, 96)
    second = reassess_risk(pregnancy)

    assert second is None
    assert pregnancy.risk_assessments.count() == 1


def test_a_worsening_level_is_recorded_with_its_previous(make_hospital, pregnancy_for):
    hospital = make_hospital("Escalating Hospital")
    pregnancy = pregnancy_for(hospital)
    add_bp(pregnancy, 145, 92, minutes_ago=10)
    reassess_risk(pregnancy)

    add_bp(pregnancy, 170, 115)
    worsened = reassess_risk(pregnancy)

    assert worsened.level == RiskAssessment.LEVEL_CRITICAL
    assert worsened.previous_level == RiskAssessment.LEVEL_MODERATE
    assert pregnancy.risk_assessments.count() == 2


def test_recovery_is_recorded_too(make_hospital, pregnancy_for):
    """Going back to stable is a transition worth keeping — it is how a
    clinician sees that an intervention worked."""
    hospital = make_hospital("Recovery Hospital")
    pregnancy = pregnancy_for(hospital)
    add_bp(pregnancy, 165, 108, minutes_ago=10)
    reassess_risk(pregnancy)

    add_bp(pregnancy, 118, 76)
    recovered = reassess_risk(pregnancy)

    assert recovered.level == RiskAssessment.LEVEL_STABLE
    assert recovered.previous_level == RiskAssessment.LEVEL_CRITICAL


def test_recording_a_reading_scores_it_immediately(client, make_hospital, pregnancy_for, auth):
    """A dangerous reading is judged as it arrives, not when a scheduler runs."""
    hospital = make_hospital("Immediate Hospital")
    pregnancy = pregnancy_for(hospital)

    response = client.post(
        f"/api/pregnancies/{pregnancy.id}/readings/",
        data=json.dumps(
            {"reading_type": "blood_pressure", "value": "172", "value_secondary": "114"},
        ),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    assert response.json()["risk_changed"] is True
    assert response.json()["risk_level"] == RiskAssessment.LEVEL_CRITICAL


# ── The queue ────────────────────────────────────────────────────────────────


def test_the_queue_lists_only_patients_needing_attention(
    client, make_hospital, pregnancy_for, auth,
):
    hospital = make_hospital("Queue Hospital")
    stable = pregnancy_for(hospital, first_name="Stable")
    at_risk = pregnancy_for(hospital, first_name="AtRisk")
    add_bp(stable, 116, 74)
    add_bp(at_risk, 166, 110)
    reassess_risk(stable)
    reassess_risk(at_risk)

    body = client.get(ATTENTION, **auth(hospital.admin.email)).json()

    assert body["count"] == 1
    assert body["results"][0]["full_name"] == "AtRisk Bibi"
    assert body["results"][0]["level"] == "critical"


def test_the_queue_puts_the_most_severe_first(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Ordering Hospital")
    moderate = pregnancy_for(hospital, first_name="Moderate")
    critical = pregnancy_for(hospital, first_name="Critical")
    add_bp(moderate, 145, 92)
    add_bp(critical, 168, 112)
    reassess_risk(moderate)
    reassess_risk(critical)

    results = client.get(ATTENTION, **auth(hospital.admin.email)).json()["results"]

    assert [r["level"] for r in results] == ["critical", "moderate"]


def test_the_queue_carries_the_reasons_and_the_responsible_clinician(
    client, make_hospital, pregnancy_for, auth,
):
    """The list is scanned, not read — it must say why without opening the record."""
    hospital = make_hospital("Reasons Hospital")
    pregnancy = pregnancy_for(hospital)
    add_bp(pregnancy, 167, 111)
    reassess_risk(pregnancy)

    row = client.get(ATTENTION, **auth(hospital.admin.email)).json()["results"][0]

    assert any("Severe hypertension" in reason for reason in row["reasons"])
    assert row["gestational_age"].endswith("d")
    # Nobody was assigned at enrolment, and the queue has to show that: an
    # unattended high-risk patient is the case most likely to be missed.
    assert row["has_responsible_clinician"] is False


def test_acknowledging_records_who_looked(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Ack Hospital")
    pregnancy = pregnancy_for(hospital)
    add_bp(pregnancy, 169, 113)
    assessment = reassess_risk(pregnancy)
    assert assessment.needs_acknowledgement is True

    response = client.post(
        f"/api/pregnancies/{pregnancy.id}/risk/{assessment.id}/acknowledge/",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200
    assessment.refresh_from_db()
    assert assessment.acknowledged_by == hospital.admin
    assert assessment.needs_acknowledgement is False


def test_an_unacknowledged_case_sorts_above_an_acknowledged_one_of_equal_severity(
    client, make_hospital, pregnancy_for, auth,
):
    hospital = make_hospital("Ack Order Hospital")
    seen = pregnancy_for(hospital, first_name="Seen")
    unseen = pregnancy_for(hospital, first_name="Unseen")
    add_bp(seen, 168, 112)
    add_bp(unseen, 169, 113)
    acknowledged = reassess_risk(seen)
    reassess_risk(unseen)

    acknowledged.acknowledged_at = timezone.now()
    acknowledged.acknowledged_by = hospital.admin
    acknowledged.save(update_fields=["acknowledged_at", "acknowledged_by"])

    results = client.get(ATTENTION, **auth(hospital.admin.email)).json()["results"]

    assert results[0]["full_name"] == "Unseen Bibi"


# ── Tenant isolation ─────────────────────────────────────────────────────────


def test_the_queue_never_crosses_hospitals(client, make_hospital, pregnancy_for, auth):
    alpha = make_hospital("Alpha Risk")
    beta = make_hospital("Beta Risk")
    beta_pregnancy = pregnancy_for(beta, first_name="BetaPatient")
    add_bp(beta_pregnancy, 170, 115)
    reassess_risk(beta_pregnancy)

    body = client.get(ATTENTION, **auth(alpha.admin.email)).json()

    assert body["count"] == 0


def test_risk_history_is_not_readable_across_hospitals(
    client, make_hospital, pregnancy_for, auth,
):
    alpha = make_hospital("Alpha History")
    beta = make_hospital("Beta History")
    beta_pregnancy = pregnancy_for(beta)
    add_bp(beta_pregnancy, 170, 115)
    reassess_risk(beta_pregnancy)

    response = client.get(
        f"/api/pregnancies/{beta_pregnancy.id}/risk/",
        **auth(alpha.admin.email),
    )

    assert response.status_code == 404
