"""``?workflow=risk_review|low_confidence`` and
``?care_activity=monitoring_follow_up|unseen_readings|reading_reminder`` on
``GET /api/patients/`` -- replaces the five standing queue endpoints that
used to live at ``/api/risk-review-queue/`` etc., consolidated into query
params on one endpoint, matching Neuro_RPM's own convention. See CLAUDE.md's
"Care Activities" section for the history of this move.

Every row also carries ``pending_risk_count``/``needs_risk_review``/
``needs_low_confidence_review`` unconditionally (not just when filtered by
``?workflow=``), matching Neuro_RPM's own "merge everything onto every row"
approach -- see ``test_patient_list_enrichment.py`` for the Care Activity
signals' own equivalent coverage.
"""

from datetime import timedelta

import pytest
from django.apps import apps as django_apps
from django.utils import timezone

from momcare_platform.core.monitoring.models import MonitoringSession
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db

PATIENTS = "/api/patients/"

# Resolved via the app registry, not a static import -- RiskAssessment lives
# in modules.pregnancy.vitals, which `core` (this test included) must never
# import statically. Same pattern test_patients.py's own
# test_the_list_carries_the_current_risk_level already uses.
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


def _flagged_high_assessment(pregnancy, *, review_status=RiskAssessment.REVIEW_PENDING):
    return RiskAssessment.objects.create(
        pregnancy=pregnancy,
        risk_level=RiskAssessment.LEVEL_HIGH,
        final_risk_level=RiskAssessment.LEVEL_HIGH,
        flagged_for_review=True,
        review_status=review_status,
    )


def _unflagged_high_assessment(pregnancy):
    return RiskAssessment.objects.create(
        pregnancy=pregnancy,
        risk_level=RiskAssessment.LEVEL_HIGH,
        final_risk_level=RiskAssessment.LEVEL_HIGH,
        flagged_for_review=False,
    )


def _flagged_low_assessment(pregnancy):
    return RiskAssessment.objects.create(
        pregnancy=pregnancy,
        risk_level=RiskAssessment.LEVEL_LOW,
        final_risk_level=RiskAssessment.LEVEL_LOW,
        flagged_for_review=True,
    )


def _normal_low_assessment(pregnancy):
    return RiskAssessment.objects.create(
        pregnancy=pregnancy,
        risk_level=RiskAssessment.LEVEL_LOW,
        final_risk_level=RiskAssessment.LEVEL_LOW,
        flagged_for_review=False,
    )


def names(body):
    return [row["full_name"] for row in body["results"]]


# ── ?workflow=risk_review ────────────────────────────────────────────────


def test_workflow_risk_review_lists_an_actionable_pending_patient(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Workflow Risk Review Hospital")
    patient = patient_for(hospital)
    _flagged_high_assessment(patient.current_pregnancy)

    response = client.get(f"{PATIENTS}?workflow=risk_review", **auth(hospital.admin.email))

    assert response.status_code == 200
    assert names(response.json()) == [patient.full_name]


def test_workflow_risk_review_excludes_already_resolved(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Resolved Excluded Hospital")
    patient = patient_for(hospital)
    _flagged_high_assessment(patient.current_pregnancy, review_status=RiskAssessment.REVIEW_REVIEWED)

    response = client.get(f"{PATIENTS}?workflow=risk_review", **auth(hospital.admin.email))

    assert response.json()["results"] == []


def test_workflow_risk_review_keeps_a_patient_listed_for_an_older_unresolved_assessment(
    client,
    make_hospital,
    patient_for,
    auth,
):
    """Matches Neuro_RPM's own reading_review_patient_condition() exactly: a
    pending reading from any past transition still counts until it is
    reviewed/escalated, even after a newer one has already been resolved."""
    hospital = make_hospital("Older Unresolved Hospital")
    patient = patient_for(hospital)
    pregnancy = patient.current_pregnancy
    RiskAssessment.objects.create(
        pregnancy=pregnancy,
        risk_level=RiskAssessment.LEVEL_MEDIUM,
        final_risk_level=RiskAssessment.LEVEL_MEDIUM,
        flagged_for_review=False,
    )
    RiskAssessment.objects.create(
        pregnancy=pregnancy,
        risk_level=RiskAssessment.LEVEL_HIGH,
        final_risk_level=RiskAssessment.LEVEL_HIGH,
        flagged_for_review=False,
        review_status=RiskAssessment.REVIEW_REVIEWED,
    )

    response = client.get(f"{PATIENTS}?workflow=risk_review", **auth(hospital.admin.email))

    body = response.json()
    assert names(body) == [patient.full_name]
    assert body["results"][0]["pending_risk_count"] == 1


def test_workflow_risk_review_includes_unflagged_actionable(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Unflagged Actionable Hospital")
    patient = patient_for(hospital)
    _unflagged_high_assessment(patient.current_pregnancy)

    response = client.get(f"{PATIENTS}?workflow=risk_review", **auth(hospital.admin.email))

    assert names(response.json()) == [patient.full_name]


def test_workflow_risk_review_excludes_flagged_low(client, make_hospital, patient_for, auth):
    """Severity-only -- a flagged Low belongs in low_confidence, not here."""
    hospital = make_hospital("Flagged Low Excluded Hospital")
    patient = patient_for(hospital)
    _flagged_low_assessment(patient.current_pregnancy)

    response = client.get(f"{PATIENTS}?workflow=risk_review", **auth(hospital.admin.email))

    assert response.json()["results"] == []


def test_workflow_risk_review_is_not_readable_across_hospitals(client, make_hospital, patient_for, auth):
    alpha = make_hospital("Alpha Workflow")
    beta = make_hospital("Beta Workflow")
    beta_patient = patient_for(beta)
    _flagged_high_assessment(beta_patient.current_pregnancy)

    response = client.get(f"{PATIENTS}?workflow=risk_review", **auth(alpha.admin.email))

    assert response.json()["results"] == []


# ── ?workflow=low_confidence ─────────────────────────────────────────────


def test_workflow_low_confidence_lists_a_flagged_patient(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Workflow Low Confidence Hospital")
    patient = patient_for(hospital)
    _flagged_high_assessment(patient.current_pregnancy)

    response = client.get(f"{PATIENTS}?workflow=low_confidence", **auth(hospital.admin.email))

    assert names(response.json()) == [patient.full_name]


def test_workflow_low_confidence_includes_flagged_low(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Flagged Low Included Hospital")
    patient = patient_for(hospital)
    _flagged_low_assessment(patient.current_pregnancy)

    response = client.get(f"{PATIENTS}?workflow=low_confidence", **auth(hospital.admin.email))

    assert names(response.json()) == [patient.full_name]


def test_workflow_low_confidence_excludes_unflagged_actionable(client, make_hospital, patient_for, auth):
    """Confidence-only -- an unflagged High belongs in risk_review, not here."""
    hospital = make_hospital("Unflagged Excluded Hospital")
    patient = patient_for(hospital)
    _unflagged_high_assessment(patient.current_pregnancy)

    response = client.get(f"{PATIENTS}?workflow=low_confidence", **auth(hospital.admin.email))

    assert response.json()["results"] == []


def test_workflow_low_confidence_excludes_normal(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Normal Excluded Hospital")
    patient = patient_for(hospital)
    _normal_low_assessment(patient.current_pregnancy)

    response = client.get(f"{PATIENTS}?workflow=low_confidence", **auth(hospital.admin.email))

    assert response.json()["results"] == []


def test_workflow_low_confidence_is_not_readable_across_hospitals(client, make_hospital, patient_for, auth):
    alpha = make_hospital("Alpha Low Confidence")
    beta = make_hospital("Beta Low Confidence")
    beta_patient = patient_for(beta)
    _flagged_low_assessment(beta_patient.current_pregnancy)

    response = client.get(f"{PATIENTS}?workflow=low_confidence", **auth(alpha.admin.email))

    assert response.json()["results"] == []


# ── Unconditional row fields (needs_risk_review/needs_low_confidence_review) ─


def test_needs_risk_review_and_low_confidence_flags_are_shown_on_every_row(
    client,
    make_hospital,
    patient_for,
    auth,
):
    """Shown on the plain, unfiltered list too -- matches Neuro_RPM's own
    convention of merging every signal onto every row unconditionally."""
    hospital = make_hospital("Unconditional Flags Hospital")
    patient = patient_for(hospital)
    _flagged_high_assessment(patient.current_pregnancy)

    response = client.get(PATIENTS, **auth(hospital.admin.email))

    row = response.json()["results"][0]
    assert row["needs_risk_review"] is True
    assert row["needs_low_confidence_review"] is True
    assert row["pending_risk_count"] == 1


def test_flags_are_false_and_zero_with_no_pending_assessment(client, make_hospital, patient_for, auth):
    hospital = make_hospital("No Flags Hospital")
    patient_for(hospital)

    response = client.get(PATIENTS, **auth(hospital.admin.email))

    row = response.json()["results"][0]
    assert row["needs_risk_review"] is False
    assert row["needs_low_confidence_review"] is False
    assert row["pending_risk_count"] == 0


# ── ?care_activity=monitoring_follow_up|unseen_readings|reading_reminder ──


def test_care_activity_monitoring_follow_up(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Care Activity Monitoring Hospital")
    patient = patient_for(hospital, with_pregnancy=False)

    response = client.get(f"{PATIENTS}?care_activity=monitoring_follow_up", **auth(hospital.admin.email))

    assert names(response.json()) == [patient.full_name]


def test_care_activity_monitoring_follow_up_excludes_a_compliant_patient(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Compliant Care Activity Hospital")
    patient = patient_for(hospital, with_pregnancy=False)
    MonitoringSession.objects.create(
        patient=patient,
        duration_seconds=1200,
        recorded_at=timezone.now(),
        added_by=hospital.admin,
    )

    response = client.get(f"{PATIENTS}?care_activity=monitoring_follow_up", **auth(hospital.admin.email))

    assert response.json()["results"] == []


def test_care_activity_reading_reminder(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Care Activity Reminder Hospital")
    patient = patient_for(hospital)

    response = client.get(f"{PATIENTS}?care_activity=reading_reminder", **auth(hospital.admin.email))

    assert names(response.json()) == [patient.full_name]


def test_care_activity_unseen_readings_excludes_a_patient_with_no_pregnancy(
    client,
    make_hospital,
    patient_for,
    auth,
):
    """unseen_readings/reading_reminder require an active pregnancy -- a
    VitalReading always needs one, unlike MonitoringSession/MonitoringNote."""
    hospital = make_hospital("No Pregnancy Care Activity Hospital")
    patient_for(hospital, with_pregnancy=False)

    response = client.get(f"{PATIENTS}?care_activity=unseen_readings", **auth(hospital.admin.email))

    assert response.json()["results"] == []


def test_unknown_care_activity_value_is_ignored(client, make_hospital, patient_for, auth):
    """An unrecognized value falls through to the unfiltered list rather
    than erroring -- matches Neuro_RPM's own tolerant query-param handling."""
    hospital = make_hospital("Unknown Value Hospital")
    patient_for(hospital)

    response = client.get(f"{PATIENTS}?care_activity=not_a_real_activity", **auth(hospital.admin.email))

    assert response.status_code == 200
    assert len(response.json()["results"]) == 1
