"""reassess_risk() wired to the real trained model.

Exercises the actual seam: a real trained artifact under momcare_model/,
real Postgres rows, the real alert path — nothing mocked, matching this
suite's convention. Vitals below were picked by querying the live model
directly so each test's expected risk_level is a fact about the trained
artifact, not a guess.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.conf import settings
from django.core import mail
from django.utils import timezone

from momcare_platform.core.alerts.models import Alert
from momcare_platform.core.monitoring.models import RiskAssessment, VitalReading
from momcare_platform.core.monitoring.services import reassess_risk
from momcare_platform.core.patients.models import Consent
from momcare_platform.core.patients.services import enrol_patient

pytestmark = pytest.mark.django_db

LOW_VITALS = {
    "age": 28,
    "systolic_bp": 118,
    "diastolic_bp": 76,
    "body_temp_f": 98.2,
    "heart_rate": 82,
    "hemoglobin": 12.1,
    "blood_glucose": 92,
    "stress_score": 3,
    "phys_activity_score": 6,
}
HIGH_VITALS = {
    "age": 35,
    "systolic_bp": 185,
    "diastolic_bp": 125,
    "body_temp_f": 103.0,
    "heart_rate": 130,
    "hemoglobin": 6.0,
    "blood_glucose": 250,
    "stress_score": 9,
    "phys_activity_score": 1,
}
# Confidently Medium (~98.5%) — high enough it must never trip the review flag.
MEDIUM_CONFIDENT_VITALS = {
    "age": 30,
    "systolic_bp": 138,
    "diastolic_bp": 88,
    "body_temp_f": 98.6,
    "heart_rate": 95,
    "hemoglobin": 10.5,
    "blood_glucose": 110,
    "stress_score": 5,
    "phys_activity_score": 4,
}
# Medium at ~54% confidence — below the 70% platform default, must trip the flag.
MEDIUM_LOW_CONFIDENCE_VITALS = {
    "age": 33,
    "systolic_bp": 118,
    "diastolic_bp": 78,
    "body_temp_f": 98.4,
    "heart_rate": 88,
    "hemoglobin": 11.2,
    "blood_glucose": 98,
    "stress_score": 4,
    "phys_activity_score": 5,
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


def test_no_reading_produces_no_assessment(make_hospital, pregnancy_for):
    hospital = make_hospital("Empty Hospital")
    pregnancy = pregnancy_for(hospital)

    assert reassess_risk(pregnancy) is None
    assert not RiskAssessment.objects.exists()


def test_healthy_vitals_score_low_and_raise_no_alert(make_hospital, pregnancy_for):
    hospital = make_hospital("Healthy Hospital")
    pregnancy = pregnancy_for(hospital)
    add_reading(pregnancy, LOW_VITALS)

    assessment = reassess_risk(pregnancy)

    assert assessment.risk_level == RiskAssessment.LEVEL_LOW
    assert assessment.final_risk_level == RiskAssessment.LEVEL_LOW
    assert assessment.flagged_for_review is False
    assert not Alert.objects.exists()


def test_dangerous_vitals_score_high_and_raise_an_alert(make_hospital, make_staff, pregnancy_for):
    hospital = make_hospital("Danger Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doctor@danger.test")
    pregnancy = pregnancy_for(hospital, clinician=doctor)
    add_reading(pregnancy, HIGH_VITALS)

    assessment = reassess_risk(pregnancy)

    assert assessment.risk_level == RiskAssessment.LEVEL_HIGH
    assert assessment.final_risk_level == RiskAssessment.LEVEL_HIGH
    alert = Alert.objects.get(pregnancy=pregnancy)
    assert alert.status == Alert.STATUS_OPEN
    assert alert.level == RiskAssessment.LEVEL_HIGH


def test_africa_medium_is_shown_as_high(make_hospital, pregnancy_for):
    hospital = make_hospital("Lagos Hospital")
    hospital.org.country = "Nigeria"
    hospital.org.save(update_fields=["country"])
    pregnancy = pregnancy_for(hospital)
    add_reading(pregnancy, MEDIUM_CONFIDENT_VITALS)

    assessment = reassess_risk(pregnancy)

    # The model's real answer is preserved even though it isn't what's shown.
    assert assessment.risk_level == RiskAssessment.LEVEL_MEDIUM
    assert assessment.final_risk_level == RiskAssessment.LEVEL_HIGH
    alert = Alert.objects.get(pregnancy=pregnancy)
    assert alert.level == RiskAssessment.LEVEL_HIGH


def test_medium_outside_africa_is_not_overridden(make_hospital, pregnancy_for):
    hospital = make_hospital("Karachi Hospital")  # make_hospital defaults to Pakistan (asia)
    pregnancy = pregnancy_for(hospital)
    add_reading(pregnancy, MEDIUM_CONFIDENT_VITALS)

    assessment = reassess_risk(pregnancy)

    assert assessment.risk_level == RiskAssessment.LEVEL_MEDIUM
    assert assessment.final_risk_level == RiskAssessment.LEVEL_MEDIUM


def test_low_confidence_flags_the_assessment_and_emails_the_doctor(make_hospital, make_staff, pregnancy_for):
    """Medium is actionable on its own, so the alert path already emails the
    assigned clinician once. Low confidence adds a second, distinct email —
    the two are independent channels and both are expected to fire here."""
    hospital = make_hospital("Uncertain Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doctor@uncertain.test")
    pregnancy = pregnancy_for(hospital, clinician=doctor)
    add_reading(pregnancy, MEDIUM_LOW_CONFIDENCE_VITALS)

    assessment = reassess_risk(pregnancy)

    assert assessment.flagged_for_review is True
    assert all(sent.to == [doctor.email] for sent in mail.outbox)  # never the patient
    review_emails = [sent for sent in mail.outbox if "review requested" in sent.subject.lower()]
    assert len(review_emails) == 1


def test_high_confidence_is_not_flagged_and_sends_no_review_email(make_hospital, make_staff, pregnancy_for):
    """Medium is still actionable, so the ordinary alert email still fires —
    what must NOT happen is the separate low-confidence review email."""
    hospital = make_hospital("Confident Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doctor@confident.test")
    pregnancy = pregnancy_for(hospital, clinician=doctor)
    add_reading(pregnancy, MEDIUM_CONFIDENT_VITALS)

    assessment = reassess_risk(pregnancy)

    assert assessment.flagged_for_review is False
    review_emails = [sent for sent in mail.outbox if "review requested" in sent.subject.lower()]
    assert len(review_emails) == 0


def test_a_hospitals_own_threshold_overrides_the_platform_default(make_hospital, make_staff, pregnancy_for):
    """MEDIUM_CONFIDENT_VITALS scores ~98.5% confidence — not flagged under the
    70% platform default (proven above). A hospital that sets its own 99%
    threshold must see the *same* result flagged instead: the override has to
    actually change behaviour, not just exist as an unused column."""
    hospital = make_hospital("Strict Hospital")
    hospital.org.confidence_threshold = Decimal("0.990")
    hospital.org.save(update_fields=["confidence_threshold"])
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doctor@strict.test")
    pregnancy = pregnancy_for(hospital, clinician=doctor)
    add_reading(pregnancy, MEDIUM_CONFIDENT_VITALS)

    assessment = reassess_risk(pregnancy)

    assert assessment.confidence < Decimal("0.990")
    assert assessment.flagged_for_review is True
    review_emails = [sent for sent in mail.outbox if "review requested" in sent.subject.lower()]
    assert len(review_emails) == 1


def test_unchanged_level_writes_no_second_assessment(make_hospital, pregnancy_for):
    hospital = make_hospital("Steady Hospital")
    pregnancy = pregnancy_for(hospital)
    add_reading(pregnancy, LOW_VITALS, minutes_ago=10)
    first = reassess_risk(pregnancy)
    add_reading(pregnancy, LOW_VITALS, minutes_ago=1)
    second = reassess_risk(pregnancy)

    assert first is not None
    assert second is None
    assert RiskAssessment.objects.filter(pregnancy=pregnancy).count() == 1


def test_worsening_creates_a_new_row_with_previous_level_recorded(make_hospital, pregnancy_for):
    hospital = make_hospital("Worsening Hospital")
    pregnancy = pregnancy_for(hospital)
    add_reading(pregnancy, LOW_VITALS, minutes_ago=10)
    reassess_risk(pregnancy)
    add_reading(pregnancy, HIGH_VITALS, minutes_ago=1)
    second = reassess_risk(pregnancy)

    assert second.previous_risk_level == RiskAssessment.LEVEL_LOW
    assert second.final_risk_level == RiskAssessment.LEVEL_HIGH
    assert RiskAssessment.objects.filter(pregnancy=pregnancy).count() == 2
