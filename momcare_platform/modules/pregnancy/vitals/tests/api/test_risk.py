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


# ── One assessment per reading ───────────────────────────────────────────────


def test_a_dangerous_reading_produces_an_assessment(make_hospital, pregnancy_for, _no_auto_scoring):
    hospital = make_hospital("Assess Hospital")
    pregnancy = pregnancy_for(hospital)
    add_reading(pregnancy, HIGH_VITALS)

    assessment = reassess_risk(pregnancy)

    assert assessment is not None
    assert assessment.final_risk_level == RiskAssessment.LEVEL_HIGH
    assert assessment.bp_category == "Hypertensive Crisis"


def test_an_unchanged_level_still_writes_its_own_row(make_hospital, pregnancy_for, _no_auto_scoring):
    """Every reading gets its own row — a full per-reading record, not a
    sparse transition log. See MEMORY.md's per-reading risk history
    decision."""
    hospital = make_hospital("No Churn Hospital")
    pregnancy = pregnancy_for(hospital)
    add_reading(pregnancy, LOW_VITALS, minutes_ago=10)
    reassess_risk(pregnancy)

    add_reading(pregnancy, LOW_VITALS)
    second = reassess_risk(pregnancy)

    assert second is not None
    assert second.final_risk_level == RiskAssessment.LEVEL_LOW
    assert pregnancy.risk_assessments.count() == 2


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


# ── History filters -- what a doctor sees after clicking into a patient from
# either queue. Matches Neuro_RPM's own generic reading-list filters
# (filterset_fields = ["patient", "reading_type", "is_out_of_range"] plus
# their hand-rolled review_status), adapted to this endpoint's own fields.
def test_risk_history_filters_to_pending_and_actionable_from_the_risk_review_queue(
    client,
    make_hospital,
    pregnancy_for,
    auth,
):
    hospital = make_hospital("History Filter Hospital")
    pregnancy = pregnancy_for(hospital)
    RiskAssessment.objects.create(
        pregnancy=pregnancy,
        risk_level=RiskAssessment.LEVEL_LOW,
        final_risk_level=RiskAssessment.LEVEL_LOW,
    )
    RiskAssessment.objects.create(
        pregnancy=pregnancy,
        risk_level=RiskAssessment.LEVEL_MEDIUM,
        final_risk_level=RiskAssessment.LEVEL_MEDIUM,
    )
    RiskAssessment.objects.create(
        pregnancy=pregnancy,
        risk_level=RiskAssessment.LEVEL_HIGH,
        final_risk_level=RiskAssessment.LEVEL_HIGH,
        review_status=RiskAssessment.REVIEW_REVIEWED,
    )

    response = client.get(
        f"/api/pregnancies/{pregnancy.id}/risk/?review_status=pending&actionable=true",
        **auth(hospital.admin.email),
    )

    body = response.json()
    # current is always the true current assessment, unaffected by the filter.
    assert body["current"]["final_risk_level"] == "high"
    # history is filtered down to just the still-pending, actionable one.
    assert len(body["history"]) == 1
    assert body["history"][0]["final_risk_level"] == "medium"


def test_risk_history_filters_to_pending_and_flagged_from_the_low_confidence_queue(
    client,
    make_hospital,
    pregnancy_for,
    auth,
):
    hospital = make_hospital("History Confidence Filter Hospital")
    pregnancy = pregnancy_for(hospital)
    RiskAssessment.objects.create(
        pregnancy=pregnancy,
        risk_level=RiskAssessment.LEVEL_LOW,
        final_risk_level=RiskAssessment.LEVEL_LOW,
        flagged_for_review=True,
    )
    RiskAssessment.objects.create(
        pregnancy=pregnancy,
        risk_level=RiskAssessment.LEVEL_HIGH,
        final_risk_level=RiskAssessment.LEVEL_HIGH,
        flagged_for_review=False,
    )

    response = client.get(
        f"/api/pregnancies/{pregnancy.id}/risk/?review_status=pending&flagged_for_review=true",
        **auth(hospital.admin.email),
    )

    body = response.json()
    assert body["current"]["final_risk_level"] == "high"
    assert len(body["history"]) == 1
    assert body["history"][0]["final_risk_level"] == "low"
    assert body["history"][0]["flagged_for_review"] is True


def test_reviewing_records_who_reviewed_it(
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
    _no_auto_scoring,
):
    """A clinician can review -- see test_hospital_admin_can_also_review_and_
    escalate below for why hospital_admin can too."""
    hospital = make_hospital("Review Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, email="doctor@reviewrisk.test")
    pregnancy = pregnancy_for(hospital)
    add_reading(pregnancy, HIGH_VITALS)
    assessment = reassess_risk(pregnancy)
    assert assessment is not None
    assert assessment.needs_review is True

    response = client.post(
        f"/api/pregnancies/{pregnancy.id}/risk/{assessment.id}/review/",
        data=json.dumps({"confirmed_risk_level": "high"}),
        content_type="application/json",
        **auth(doctor.email),
    )

    assert response.status_code == 200
    assessment.refresh_from_db()
    assert assessment.verified_by == doctor
    assert assessment.review_status == RiskAssessment.REVIEW_REVIEWED
    assert assessment.needs_review is False


def test_hospital_admin_can_also_review_and_escalate(
    client,
    make_hospital,
    pregnancy_for,
    auth,
    _no_auto_scoring,
):
    """Matches Neuro_RPM's own permission for the identical action exactly
    (MANAGE = IsAdmin | IsCareManager on their reading review/escalate) --
    found via a permission audit to be a real, unintentional gap: this view
    had reused the plain IsClinician class (excludes admin), whose reasoning
    was written for Alert acknowledgment specifically, not this action.
    Alert acknowledge/resolve itself is unaffected -- still IsClinician-only."""
    hospital = make_hospital("Admin Review Hospital")
    pregnancy_a = pregnancy_for(hospital, first_name="Amina")
    pregnancy_b = pregnancy_for(hospital, first_name="Bushra")
    add_reading(pregnancy_a, HIGH_VITALS)
    add_reading(pregnancy_b, HIGH_VITALS)
    assessment_a = reassess_risk(pregnancy_a)
    assessment_b = reassess_risk(pregnancy_b)
    assert assessment_a is not None
    assert assessment_b is not None

    review_response = client.post(
        f"/api/pregnancies/{pregnancy_a.id}/risk/{assessment_a.id}/review/",
        data=json.dumps({"confirmed_risk_level": "high"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )
    escalate_response = client.post(
        f"/api/pregnancies/{pregnancy_b.id}/risk/{assessment_b.id}/escalate/",
        data=json.dumps({"confirmed_risk_level": "high"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert review_response.status_code == 200
    assert escalate_response.status_code == 200
    assessment_a.refresh_from_db()
    assessment_b.refresh_from_db()
    assert assessment_a.review_status == RiskAssessment.REVIEW_REVIEWED
    assert assessment_b.review_status == RiskAssessment.REVIEW_ESCALATED


def test_escalating_is_a_label_only_no_alert_side_effect(
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
    _no_auto_scoring,
):
    """Matches Neuro_RPM's own escalate() exactly: only the status changes.
    The real Alert ladder is untouched by this action."""
    hospital = make_hospital("Escalate Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, email="doctor@escalaterisk.test")
    pregnancy = pregnancy_for(hospital, clinician=doctor)
    add_reading(pregnancy, HIGH_VITALS)
    assessment = reassess_risk(pregnancy)
    assert assessment is not None
    alert = Alert.objects.get(pregnancy=pregnancy)
    tier_before = alert.tier

    response = client.post(
        f"/api/pregnancies/{pregnancy.id}/risk/{assessment.id}/escalate/",
        data=json.dumps({"confirmed_risk_level": "high"}),
        content_type="application/json",
        **auth(doctor.email),
    )

    assert response.status_code == 200
    assessment.refresh_from_db()
    assert assessment.review_status == RiskAssessment.REVIEW_ESCALATED
    alert.refresh_from_db()
    assert alert.tier == tier_before
    assert alert.events.filter(kind="escalated", actor=doctor).count() == 0


def test_a_resolved_assessment_cannot_be_reviewed_again(
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
    _no_auto_scoring,
):
    hospital = make_hospital("Already Resolved Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, email="doctor@resolved.test")
    pregnancy = pregnancy_for(hospital)
    add_reading(pregnancy, HIGH_VITALS)
    assessment = reassess_risk(pregnancy)
    assert assessment is not None
    client.post(
        f"/api/pregnancies/{pregnancy.id}/risk/{assessment.id}/review/",
        data=json.dumps({"confirmed_risk_level": "high"}),
        content_type="application/json",
        **auth(doctor.email),
    )

    response = client.post(
        f"/api/pregnancies/{pregnancy.id}/risk/{assessment.id}/escalate/",
        data=json.dumps({"confirmed_risk_level": "high"}),
        content_type="application/json",
        **auth(doctor.email),
    )

    assert response.status_code == 400
    assert "already been resolved" in response.json()["detail"]


def test_an_unflagged_low_risk_assessment_cannot_be_reviewed(
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
    _no_auto_scoring,
):
    """Neither condition applies -- not actionable, not flagged -- so this
    never needed attention in the first place."""
    hospital = make_hospital("Low Risk Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, email="doctor@lowrisk.test")
    pregnancy = pregnancy_for(hospital)
    add_reading(pregnancy, LOW_VITALS)
    assessment = reassess_risk(pregnancy)
    assert assessment is not None
    assert assessment.is_actionable is False
    assert assessment.flagged_for_review is False

    response = client.post(
        f"/api/pregnancies/{pregnancy.id}/risk/{assessment.id}/review/",
        data=json.dumps({"confirmed_risk_level": "low"}),
        content_type="application/json",
        **auth(doctor.email),
    )

    assert response.status_code == 400
    assert "actionable" in response.json()["detail"]


def test_a_flagged_low_risk_assessment_can_be_reviewed(
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
):
    """Not actionable, but flagged -- the model itself was unsure about a
    Low verdict, so it's still worth a doctor's look and a real review
    action, not a dead end."""
    hospital = make_hospital("Flagged Low Risk Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, email="doctor@flaggedlow.test")
    pregnancy = pregnancy_for(hospital)
    assessment = RiskAssessment.objects.create(
        pregnancy=pregnancy,
        risk_level=RiskAssessment.LEVEL_LOW,
        final_risk_level=RiskAssessment.LEVEL_LOW,
        flagged_for_review=True,
    )
    assert assessment.needs_review is True

    response = client.post(
        f"/api/pregnancies/{pregnancy.id}/risk/{assessment.id}/review/",
        data=json.dumps({"confirmed_risk_level": "low"}),
        content_type="application/json",
        **auth(doctor.email),
    )

    assert response.status_code == 200
    assessment.refresh_from_db()
    assert assessment.review_status == RiskAssessment.REVIEW_REVIEWED


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


def _flagged_high_assessment(pregnancy, *, review_status=RiskAssessment.REVIEW_PENDING):
    return RiskAssessment.objects.create(
        pregnancy=pregnancy,
        risk_level=RiskAssessment.LEVEL_HIGH,
        final_risk_level=RiskAssessment.LEVEL_HIGH,
        flagged_for_review=True,
        review_status=review_status,
    )


# ── Bulk review ──────────────────────────────────────────────────────────────
def test_hospital_admin_can_bulk_review(client, make_hospital, pregnancy_for, auth):
    """Same permission as the single-assessment actions -- see
    test_hospital_admin_can_also_review_and_escalate for the full reasoning."""
    hospital = make_hospital("Admin Bulk Review Hospital")
    pregnancy = pregnancy_for(hospital)
    assessment = _flagged_high_assessment(pregnancy)

    response = client.post(
        "/api/risk/bulk-review/",
        data=json.dumps(
            {
                "items": [
                    {"assessment_id": str(assessment.id), "review_status": "reviewed", "confirmed_risk_level": "high"}
                ]
            },
        ),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200
    assessment.refresh_from_db()
    assert assessment.review_status == RiskAssessment.REVIEW_REVIEWED


def test_bulk_review_resolves_each_item_to_its_own_status(
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
):
    hospital = make_hospital("Bulk Review Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, email="doctor@bulkreview.test")
    pregnancy_a = pregnancy_for(hospital, first_name="Amina")
    pregnancy_b = pregnancy_for(hospital, first_name="Bushra")
    assessment_a = _flagged_high_assessment(pregnancy_a)
    assessment_b = _flagged_high_assessment(pregnancy_b)

    response = client.post(
        "/api/risk/bulk-review/",
        data=json.dumps(
            {
                "items": [
                    {
                        "assessment_id": str(assessment_a.id),
                        "review_status": "reviewed",
                        "confirmed_risk_level": "high",
                    },
                    {
                        "assessment_id": str(assessment_b.id),
                        "review_status": "escalated",
                        "confirmed_risk_level": "high",
                    },
                ],
            },
        ),
        content_type="application/json",
        **auth(doctor.email),
    )

    assert response.status_code == 200
    assessment_a.refresh_from_db()
    assessment_b.refresh_from_db()
    assert assessment_a.review_status == RiskAssessment.REVIEW_REVIEWED
    assert assessment_b.review_status == RiskAssessment.REVIEW_ESCALATED
    assert assessment_a.verified_by == doctor
    assert assessment_b.verified_by == doctor


def test_bulk_review_rolls_back_entirely_on_one_bad_item(
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
):
    hospital = make_hospital("Bulk Rollback Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, email="doctor@bulkrollback.test")
    pregnancy_a = pregnancy_for(hospital, first_name="Amina")
    pregnancy_b = pregnancy_for(hospital, first_name="Bushra")
    assessment_a = _flagged_high_assessment(pregnancy_a)
    already_resolved = _flagged_high_assessment(pregnancy_b, review_status=RiskAssessment.REVIEW_REVIEWED)

    response = client.post(
        "/api/risk/bulk-review/",
        data=json.dumps(
            {
                "items": [
                    {
                        "assessment_id": str(assessment_a.id),
                        "review_status": "reviewed",
                        "confirmed_risk_level": "high",
                    },
                    {
                        "assessment_id": str(already_resolved.id),
                        "review_status": "reviewed",
                        "confirmed_risk_level": "high",
                    },
                ],
            },
        ),
        content_type="application/json",
        **auth(doctor.email),
    )

    assert response.status_code == 400
    assessment_a.refresh_from_db()
    assert assessment_a.review_status == RiskAssessment.REVIEW_PENDING


def test_bulk_review_cannot_touch_another_hospitals_assessment(
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
):
    alpha = make_hospital("Alpha Bulk")
    beta = make_hospital("Beta Bulk")
    doctor = make_staff(alpha.org, settings.ROLE_PROVIDER, email="doctor@alphabulk.test")
    beta_pregnancy = pregnancy_for(beta)
    beta_assessment = _flagged_high_assessment(beta_pregnancy)

    response = client.post(
        "/api/risk/bulk-review/",
        data=json.dumps(
            {
                "items": [
                    {
                        "assessment_id": str(beta_assessment.id),
                        "review_status": "reviewed",
                        "confirmed_risk_level": "high",
                    },
                ],
            },
        ),
        content_type="application/json",
        **auth(doctor.email),
    )

    assert response.status_code == 400
    assert "not found" in response.json()["detail"]
