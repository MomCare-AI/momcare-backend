"""The automatic scoring signal (vitals/signals.py) — proven on its own.

test_reassess_risk.py and test_risk.py both disconnect this signal, because
they test ``reassess_risk()``/the API by calling it explicitly. This file is
the other half: proof that creating a ``VitalReading`` through the plain ORM
— no view, no explicit ``reassess_risk()`` call — gets scored anyway.
"""

from datetime import timedelta

import pytest
from django.db.models.signals import post_save
from django.utils import timezone

from momcare_platform.core.patients.services import onboard_patient
from momcare_platform.modules.pregnancy.alerts.models import Alert
from momcare_platform.modules.pregnancy.vitals.models import RiskAssessment, VitalReading
from momcare_platform.modules.pregnancy.vitals.signals import score_reading_on_save

pytestmark = pytest.mark.django_db

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
def pregnancy(make_hospital):
    hospital = make_hospital("Signal Hospital")
    patient = onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
        pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=28)},
    )
    return patient.current_pregnancy


def test_creating_a_reading_through_the_plain_orm_scores_it(pregnancy):
    """No view, no explicit reassess_risk() call — just VitalReading.objects
    .create(), the same call any future write path would make."""
    assert not RiskAssessment.objects.filter(pregnancy=pregnancy).exists()

    VitalReading.objects.create(
        pregnancy=pregnancy,
        recorded_at=timezone.now(),
        source=VitalReading.SOURCE_MANUAL,
        **HIGH_VITALS,
    )

    assessment = RiskAssessment.objects.get(pregnancy=pregnancy)
    assert assessment.final_risk_level == RiskAssessment.LEVEL_HIGH


def test_it_also_raises_the_alert_not_just_the_assessment(pregnancy):
    """Scoring and alerting are one transaction (reassess_risk()'s own
    docstring) — the signal must not have split that apart."""
    VitalReading.objects.create(
        pregnancy=pregnancy,
        recorded_at=timezone.now(),
        source=VitalReading.SOURCE_MANUAL,
        **HIGH_VITALS,
    )

    alert = Alert.objects.get(pregnancy=pregnancy)
    assert alert.status == Alert.STATUS_OPEN
    assert alert.level == RiskAssessment.LEVEL_HIGH


def test_updating_an_existing_reading_does_not_rescore(pregnancy):
    """post_save's ``created`` flag is what gates this — an update must not
    silently re-trigger scoring."""
    reading = VitalReading.objects.create(
        pregnancy=pregnancy,
        recorded_at=timezone.now(),
        source=VitalReading.SOURCE_MANUAL,
        **HIGH_VITALS,
    )
    assert RiskAssessment.objects.filter(pregnancy=pregnancy).count() == 1

    reading.stress_score = 2
    reading.save(update_fields=["stress_score"])

    assert RiskAssessment.objects.filter(pregnancy=pregnancy).count() == 1


def test_bulk_create_does_not_trigger_it(pregnancy):
    """Documented, known Django behaviour — post_save never fires for
    bulk_create(), for any model. Asserted here so a future bulk-reading
    import path is never assumed to be scored just because this signal
    exists; whoever builds one must call reassess_risk() explicitly."""
    VitalReading.objects.bulk_create(
        [
            VitalReading(
                pregnancy=pregnancy,
                recorded_at=timezone.now(),
                source=VitalReading.SOURCE_MANUAL,
                **HIGH_VITALS,
            ),
        ],
    )

    assert not RiskAssessment.objects.filter(pregnancy=pregnancy).exists()


def test_disconnecting_the_signal_proves_it_was_doing_the_work(pregnancy):
    """Fault injection: with the receiver removed, the exact same create()
    call that scores a reading everywhere else in this file must not."""
    post_save.disconnect(score_reading_on_save, sender=VitalReading)
    try:
        VitalReading.objects.create(
            pregnancy=pregnancy,
            recorded_at=timezone.now(),
            source=VitalReading.SOURCE_MANUAL,
            **HIGH_VITALS,
        )
        assert not RiskAssessment.objects.filter(pregnancy=pregnancy).exists()
    finally:
        post_save.connect(score_reading_on_save, sender=VitalReading)
