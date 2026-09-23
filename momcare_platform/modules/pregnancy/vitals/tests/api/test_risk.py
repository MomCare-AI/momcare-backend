"""Risk assessment through the API — persistence, verification, and the
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

from momcare_platform.core.patients.services import onboard_patient
from momcare_platform.modules.pregnancy.alerts.models import Alert
from momcare_platform.modules.pregnancy.vitals.models import RiskAssessment, VitalReading
from momcare_platform.modules.pregnancy.vitals.services import reassess_risk

pytestmark = pytest.mark.django_db


LOW_VITALS = {
    "systolic_bp": 118,
    "diastolic_bp": 76,
    "heart_rate": 82,
    "body_temp_f": 98.2,
    "hemoglobin": 12.1,
    "blood_glucose": 92,
    "stress_score": 3,
    "phys_activity_score": 6,
}
MEDIUM_VITALS = {
    "systolic_bp": 138,
    "diastolic_bp": 88,
    "heart_rate": 95,
    "body_temp_f": 98.6,
    "hemoglobin": 10.5,
    "blood_glucose": 110,
    "stress_score": 5,
    "phys_activity_score": 4,
}
HIGH_VITALS = {
    "systolic_bp": 185,
    "diastolic_bp": 125,
    "heart_rate": 130,
    "body_temp_f": 103.0,
    "hemoglobin": 6.0,
    "blood_glucose": 250,
    "stress_score": 9,
    "phys_activity_score": 1,
}


@pytest.fixture
def _no_auto_scoring():
    """See test_reassess_risk.py's fixture of the same name — same reason:
    tests below that create a reading via ``add_reading()`` and then call
    ``reassess_risk()`` by hand need the automatic post_save scoring
    disconnected, or the signal scores it first and the explicit call finds
    nothing changed. Deliberately NOT ``autouse``:
    ``test_recording_a_reading_scores_it_immediately`` posts through the
    real view and needs the signal connected to prove the real end-to-end
    behaviour. Proven on its own in test_signals.py."""
    from django.db.models.signals import post_save

    from momcare_platform.modules.pregnancy.vitals.signals import score_reading_on_save

    post_save.disconnect(score_reading_on_save, sender=VitalReading)
    yield
    post_save.connect(score_reading_on_save, sender=VitalReading)


@pytest.fixture
def pregnancy_for(db):
    def _make(hospital, *, first_name="Ayesha", clinician=None):
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
            pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=28)},
        )
        pregnancy = patient.current_pregnancy
        if clinician is not None:
            pregnancy.provider = clinician.staff
            pregnancy.save(update_fields=["provider", "updated_at"])
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


def test_a_dangerous_reading_produces_an_assessment(make_hospital, pregnancy_for, _no_auto_scoring):
    hospital = make_hospital("Assess Hospital")
    pregnancy = pregnancy_for(hospital)
    add_reading(pregnancy, HIGH_VITALS)

    assessment = reassess_risk(pregnancy)

    assert assessment is not None
    assert assessment.final_risk_level == RiskAssessment.LEVEL_HIGH
    assert assessment.bp_category == "Hypertensive Crisis"


def test_an_unchanged_level_does_not_write_another_row(make_hospital, pregnancy_for, _no_auto_scoring):
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


def test_a_worsening_level_is_recorded_with_its_previous(make_hospital, pregnancy_for, _no_auto_scoring):
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


def test_recovery_is_recorded_too(make_hospital, pregnancy_for, _no_auto_scoring):
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
def test_the_clinical_categories_are_reachable_via_the_risk_endpoint(
    client,
    make_hospital,
    pregnancy_for,
    auth,
    _no_auto_scoring,
):
    """The five display-only vital categories live on the assessment itself,
    one call away from the reading that produced them."""
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
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
    _no_auto_scoring,
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


def test_acknowledging_the_alert_records_who_looked(
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
    _no_auto_scoring,
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
def test_risk_history_is_not_readable_across_hospitals(
    client,
    make_hospital,
    pregnancy_for,
    auth,
    _no_auto_scoring,
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
