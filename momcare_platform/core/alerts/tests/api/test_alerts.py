"""The alert lifecycle: raised, escalated, answered, closed.

The attention queue only works while somebody is looking at a screen. These
tests cover the part that has to work when nobody is.
"""

import json
from datetime import timedelta

import pytest
from django.conf import settings
from django.core import mail
from django.db import IntegrityError, transaction
from django.utils import timezone

from momcare_platform.core.alerts import escalation
from momcare_platform.core.alerts.models import Alert, AlertEvent
from momcare_platform.core.alerts.services import escalate_due_alerts
from momcare_platform.core.monitoring.models import VitalReading
from momcare_platform.core.monitoring.services import reassess_risk
from momcare_platform.core.patients.models import Consent
from momcare_platform.core.patients.services import enrol_patient

pytestmark = pytest.mark.django_db

ALERTS = "/api/alerts/"


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


def add_bp(pregnancy, systolic, diastolic, *, minutes_ago=1):
    VitalReading.objects.create(
        pregnancy=pregnancy,
        reading_type=VitalReading.TYPE_BLOOD_PRESSURE,
        value=systolic,
        value_secondary=diastolic,
        recorded_at=timezone.now() - timedelta(minutes=minutes_ago),
        source=VitalReading.SOURCE_MANUAL,
    )


def go_critical(pregnancy):
    add_bp(pregnancy, 168, 112)
    return reassess_risk(pregnancy)


def go_moderate(pregnancy):
    add_bp(pregnancy, 145, 92)
    return reassess_risk(pregnancy)


def recover(pregnancy):
    add_bp(pregnancy, 116, 74)
    return reassess_risk(pregnancy)


# -- Raising -------------------------------------------------------------------


def test_a_dangerous_reading_raises_an_alert(make_hospital, pregnancy_for):
    """Raising happens inside the request that recorded the reading, so a
    dangerous value is escalated immediately rather than when a job next runs."""
    hospital = make_hospital("Raise Hospital")
    pregnancy = pregnancy_for(hospital)

    go_critical(pregnancy)

    alert = Alert.objects.get(pregnancy=pregnancy)
    assert alert.status == Alert.STATUS_OPEN
    assert alert.level == "critical"
    assert "preeclampsia" in alert.reasons[0].lower()


def test_a_stable_patient_raises_nothing(make_hospital, pregnancy_for):
    hospital = make_hospital("Quiet Hospital")
    pregnancy = pregnancy_for(hospital)

    add_bp(pregnancy, 115, 74)
    reassess_risk(pregnancy)

    assert not Alert.objects.exists()


def test_a_second_alert_is_never_opened_while_one_is_live(make_hospital, pregnancy_for):
    """Alert fatigue is what kills clinical alerting systems. Two rows for the
    same deteriorating patient trains people to dismiss both."""
    hospital = make_hospital("Fatigue Hospital")
    pregnancy = pregnancy_for(hospital)
    go_moderate(pregnancy)

    go_critical(pregnancy)
    recover(pregnancy)
    go_critical(pregnancy)

    assert Alert.objects.filter(pregnancy=pregnancy, status__in=Alert.LIVE_STATUSES).count() == 1


def test_the_database_refuses_a_second_live_alert(make_hospital, pregnancy_for):
    """Belt and braces: the service enforces this, and so does the schema, so a
    future code path that forgets cannot create the state."""
    hospital = make_hospital("Constraint Hospital")
    pregnancy = pregnancy_for(hospital)
    first = go_critical(pregnancy)

    with pytest.raises(IntegrityError), transaction.atomic():
        Alert.objects.create(pregnancy=pregnancy, assessment=first, level="critical")


# -- Worsening -----------------------------------------------------------------


def test_worsening_sharpens_the_existing_alert(make_hospital, pregnancy_for):
    hospital = make_hospital("Worsen Hospital")
    pregnancy = pregnancy_for(hospital)
    go_moderate(pregnancy)

    go_critical(pregnancy)

    alert = Alert.objects.get(pregnancy=pregnancy)
    assert alert.level == "critical"
    assert alert.events.filter(kind=AlertEvent.KIND_WORSENED).exists()


def test_worsening_undoes_an_acknowledgement(make_hospital, pregnancy_for, make_staff):
    """A clinician who accepted "moderate" has not accepted "critical". Leaving
    the acknowledgement in place would stop the clock on a question nobody has
    actually answered."""
    hospital = make_hospital("Reset Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doc@reset.test")
    pregnancy = pregnancy_for(hospital, clinician=doctor)
    go_moderate(pregnancy)

    alert = Alert.objects.get(pregnancy=pregnancy)
    alert.status = Alert.STATUS_ACKNOWLEDGED
    alert.acknowledged_at = timezone.now()
    alert.acknowledged_by = doctor
    alert.save()

    go_critical(pregnancy)

    alert.refresh_from_db()
    assert alert.status == Alert.STATUS_OPEN
    assert alert.acknowledged_at is None
    assert alert.acknowledged_by is None


# -- Recovery ------------------------------------------------------------------


def test_returning_to_stable_closes_the_alert(make_hospital, pregnancy_for):
    hospital = make_hospital("Recover Hospital")
    pregnancy = pregnancy_for(hospital)
    go_critical(pregnancy)

    recover(pregnancy)

    alert = Alert.objects.get(pregnancy=pregnancy)
    assert alert.status == Alert.STATUS_RESOLVED
    assert alert.resolution == Alert.RESOLUTION_RECOVERED
    # Closed by the system, so no person is credited with the decision.
    assert alert.resolved_by is None


# -- Escalation ----------------------------------------------------------------


def test_an_unanswered_alert_climbs_when_its_deadline_passes(
    make_hospital, pregnancy_for, make_staff,
):
    hospital = make_hospital("Climb Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doc@climb.test")
    pregnancy = pregnancy_for(hospital, clinician=doctor)
    go_critical(pregnancy)

    alert = Alert.objects.get(pregnancy=pregnancy)
    assert alert.tier == escalation.TIER_CLINICIAN

    moved = escalate_due_alerts(now=timezone.now() + timedelta(minutes=6))

    alert.refresh_from_db()
    assert moved == 1
    assert alert.tier == escalation.TIER_WARD
    assert alert.events.filter(kind=AlertEvent.KIND_ESCALATED).exists()


def test_acknowledging_stops_the_ladder(client, make_hospital, pregnancy_for, make_staff, auth):
    """The point of climbing is to find somebody who will look. Somebody has."""
    hospital = make_hospital("Stop Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doc@stop.test")
    pregnancy = pregnancy_for(hospital, clinician=doctor)
    go_critical(pregnancy)
    alert = Alert.objects.get(pregnancy=pregnancy)

    client.post(f"{ALERTS}{alert.id}/acknowledge/", **auth(doctor.email))
    moved = escalate_due_alerts(now=timezone.now() + timedelta(hours=3))

    alert.refresh_from_db()
    assert moved == 0
    assert alert.tier == escalation.TIER_CLINICIAN
    assert alert.next_escalation_at is None


def test_the_sweep_is_idempotent(make_hospital, pregnancy_for, make_staff):
    """Safe to run every minute: a second sweep at the same moment must do
    nothing, or the event history fills with phantom escalations."""
    hospital = make_hospital("Idempotent Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doc@idem.test")
    pregnancy = pregnancy_for(hospital, clinician=doctor)
    go_critical(pregnancy)

    later = timezone.now() + timedelta(minutes=6)
    assert escalate_due_alerts(now=later) == 1
    assert escalate_due_alerts(now=later) == 0


def test_a_late_sweep_jumps_straight_to_the_right_tier(
    make_hospital, pregnancy_for, make_staff,
):
    """A scheduler outage must not silently under-escalate."""
    hospital = make_hospital("Late Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doc@late.test")
    pregnancy = pregnancy_for(hospital, clinician=doctor)
    go_critical(pregnancy)

    escalate_due_alerts(now=timezone.now() + timedelta(hours=2))

    alert = Alert.objects.get(pregnancy=pregnancy)
    assert alert.tier == escalation.TIER_ADMIN


# -- Recipients ----------------------------------------------------------------


def test_an_unassigned_pregnancy_escalates_immediately(make_hospital, pregnancy_for):
    """Nobody is on the first rung, so waiting out its deadline would mean a
    critical patient sat unnotified for five minutes for no reason."""
    hospital = make_hospital("Unassigned Hospital")
    pregnancy = pregnancy_for(hospital)

    go_critical(pregnancy)

    alert = Alert.objects.get(pregnancy=pregnancy)
    assert alert.tier > escalation.TIER_CLINICIAN
    assert alert.events.filter(
        kind=AlertEvent.KIND_NOTIFIED,
        detail__icontains="no recipient",
    ).exists()


def test_a_departed_clinician_is_not_a_valid_recipient(
    make_hospital, pregnancy_for, make_staff,
):
    """Staff are soft-deleted, so a departure leaves the pregnancy still
    *looking* assigned. Treating that as delivery would route a critical alert
    to nobody and record it as sent."""
    hospital = make_hospital("Departed Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "gone@departed.test")
    pregnancy = pregnancy_for(hospital, clinician=doctor)
    doctor.staff.is_active = False
    doctor.staff.save(update_fields=["is_active"])

    go_critical(pregnancy)

    alert = Alert.objects.get(pregnancy=pregnancy)
    assert alert.tier > escalation.TIER_CLINICIAN


def test_the_assigned_clinician_is_emailed(make_hospital, pregnancy_for, make_staff):
    hospital = make_hospital("Mail Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doc@mail.test")
    pregnancy = pregnancy_for(hospital, clinician=doctor)
    mail.outbox.clear()

    go_critical(pregnancy)

    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [doctor.email]
    assert "CRITICAL" in mail.outbox[0].subject


def test_an_undeliverable_address_does_not_lose_the_alert(
    make_hospital, pregnancy_for, make_staff,
):
    """Email is the second attempt at reaching someone; the in-portal alert is
    the first. An address that cannot be sent to must never mean no alert
    exists — that would trade a visible problem for an invisible one."""
    hospital = make_hospital("Resilient Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doc@resilient.test")
    pregnancy = pregnancy_for(hospital, clinician=doctor)
    doctor.email = ""
    doctor.save(update_fields=["email"])

    go_critical(pregnancy)

    assert Alert.objects.filter(pregnancy=pregnancy, status=Alert.STATUS_OPEN).exists()


# -- The API -------------------------------------------------------------------


def test_the_list_shows_live_alerts_with_the_patient_inline(
    client, make_hospital, pregnancy_for, auth,
):
    hospital = make_hospital("List Hospital")
    pregnancy = pregnancy_for(hospital, first_name="Zainab")
    go_critical(pregnancy)

    body = client.get(ALERTS, **auth(hospital.admin.email)).json()

    assert body["count"] == 1
    row = body["results"][0]
    assert row["patient_name"] == "Zainab Bibi"
    assert row["level"] == "critical"
    assert row["reasons"]
    assert body["unacknowledged"] == 1


def test_the_list_puts_the_most_severe_first(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Order Hospital")
    mild = pregnancy_for(hospital, first_name="Mild")
    severe = pregnancy_for(hospital, first_name="Severe")
    go_moderate(mild)
    go_critical(severe)

    results = client.get(ALERTS, **auth(hospital.admin.email)).json()["results"]

    assert [r["level"] for r in results] == ["critical", "moderate"]


def test_resolved_alerts_are_asked_for_explicitly(client, make_hospital, pregnancy_for, auth):
    """Kept, because the record of what happened is the point — but out of the
    way of the list somebody is working from."""
    hospital = make_hospital("History Hospital")
    pregnancy = pregnancy_for(hospital)
    go_critical(pregnancy)
    recover(pregnancy)
    headers = auth(hospital.admin.email)

    assert client.get(ALERTS, **headers).json()["count"] == 0
    assert client.get(f"{ALERTS}?status=resolved", **headers).json()["count"] == 1


def test_acknowledging_through_the_api_records_who_looked(
    client, make_hospital, make_staff, pregnancy_for, auth,
):
    hospital = make_hospital("Ack Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, email="doctor@ack.test")
    pregnancy = pregnancy_for(hospital)
    go_critical(pregnancy)
    alert = Alert.objects.get(pregnancy=pregnancy)

    response = client.post(f"{ALERTS}{alert.id}/acknowledge/", **auth(doctor.email))

    assert response.status_code == 200
    alert.refresh_from_db()
    assert alert.status == Alert.STATUS_ACKNOWLEDGED
    assert alert.acknowledged_by == doctor


def test_resolving_closes_the_episode(client, make_hospital, make_staff, pregnancy_for, auth):
    hospital = make_hospital("Close Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, email="doctor@close.test")
    pregnancy = pregnancy_for(hospital)
    go_critical(pregnancy)
    alert = Alert.objects.get(pregnancy=pregnancy)

    response = client.post(
        f"{ALERTS}{alert.id}/resolve/",
        data=json.dumps({"resolution": "handled"}),
        content_type="application/json",
        **auth(doctor.email),
    )

    assert response.status_code == 200
    alert.refresh_from_db()
    assert alert.status == Alert.STATUS_RESOLVED
    assert alert.resolved_by == doctor


def test_an_already_closed_alert_cannot_be_closed_again(
    client, make_hospital, make_staff, pregnancy_for, auth,
):
    hospital = make_hospital("Twice Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, email="doctor@twice.test")
    pregnancy = pregnancy_for(hospital)
    go_critical(pregnancy)
    recover(pregnancy)
    alert = Alert.objects.get(pregnancy=pregnancy)

    response = client.post(f"{ALERTS}{alert.id}/resolve/", **auth(doctor.email))

    assert response.status_code == 400


def test_the_detail_view_carries_the_whole_history(client, make_hospital, pregnancy_for, auth):
    """Escalation that is not written down is indistinguishable from escalation
    that never happened."""
    hospital = make_hospital("Trail Hospital")
    pregnancy = pregnancy_for(hospital)
    go_critical(pregnancy)
    alert = Alert.objects.get(pregnancy=pregnancy)

    body = client.get(f"{ALERTS}{alert.id}/", **auth(hospital.admin.email)).json()

    kinds = [event["kind"] for event in body["events"]]
    assert AlertEvent.KIND_RAISED in kinds
    assert AlertEvent.KIND_NOTIFIED in kinds


# -- Tenant isolation ----------------------------------------------------------


def test_alerts_never_cross_hospitals(client, make_hospital, pregnancy_for, auth):
    alpha = make_hospital("Alpha Alerts")
    beta = make_hospital("Beta Alerts")
    go_critical(pregnancy_for(beta))

    body = client.get(ALERTS, **auth(alpha.admin.email)).json()

    assert body["count"] == 0


def test_another_hospitals_alert_cannot_be_acknowledged(
    client, make_hospital, make_staff, pregnancy_for, auth,
):
    """404, never 403 — a 403 would confirm the alert exists somewhere else.

    Asked as a clinician, because that is now the only role the endpoint
    admits: an administrator is refused for lacking the role, which answers a
    different question and would not exercise the tenant boundary at all.
    """
    alpha = make_hospital("Alpha Ack")
    beta = make_hospital("Beta Ack")
    outsider = make_staff(alpha.org, settings.ROLE_PROVIDER, email="doctor@alpha.test")
    go_critical(pregnancy_for(beta))
    alert = Alert.objects.first()

    response = client.post(f"{ALERTS}{alert.id}/acknowledge/", **auth(outsider.email))

    assert response.status_code == 404


def test_a_patient_cannot_read_the_alert_list(client, make_hospital, auth):
    """Alerts are a clinical worklist naming other people's patients. A patient
    account belongs to the same hospital, so tenant scoping alone would let her
    read it — the role gate is what stops that."""
    from momcare_platform.core.users.models import Role, User  # noqa: PLC0415

    hospital = make_hospital("Patient Block")
    mother = User.objects.create_user(
        email="mother@block.test",
        password="MotherPass!2026",
        first_name="Mother",
        last_name="Block",
        role=Role.objects.get(code=settings.ROLE_PATIENT),
    )
    mother.organization = hospital.org
    mother.save(update_fields=["organization", "updated_at"])

    response = client.get(ALERTS, **auth(mother.email, "MotherPass!2026"))

    assert response.status_code == 403


def test_reading_alerts_is_audited(client, make_hospital, pregnancy_for, auth):
    """Alerts carry patient names and clinical findings, so reading them is
    access to PHI exactly as reading the record is."""
    from momcare_platform.core.organization.models import AuditLog  # noqa: PLC0415

    hospital = make_hospital("Audit Alerts")
    go_critical(pregnancy_for(hospital))

    client.get(ALERTS, **auth(hospital.admin.email))

    entry = AuditLog.objects.filter(resource="alerts", action="READ").first()
    assert entry is not None, "alert access was not written to the audit log"
    assert entry.user == hospital.admin
