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

from momcare_platform.core.patients.services import onboard_patient
from momcare_platform.modules.pregnancy.alerts import escalation
from momcare_platform.modules.pregnancy.alerts.models import Alert, AlertEvent
from momcare_platform.modules.pregnancy.alerts.services import escalate_due_alerts
from momcare_platform.modules.pregnancy.vitals.models import VitalReading
from momcare_platform.modules.pregnancy.vitals.services import reassess_risk

pytestmark = pytest.mark.django_db

ALERTS = "/api/alerts/"


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


# Real, verified vitals combinations — the same ones proven against the
# actual trained model in test_reassess_risk.py — not arbitrary numbers.
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


def add_reading(pregnancy, vitals, *, minutes_ago=1):
    return VitalReading.objects.create(
        pregnancy=pregnancy,
        recorded_at=timezone.now() - timedelta(minutes=minutes_ago),
        source=VitalReading.SOURCE_MANUAL,
        **vitals,
    )


def go_critical(pregnancy):
    add_reading(pregnancy, HIGH_VITALS)
    return reassess_risk(pregnancy)


def go_moderate(pregnancy):
    add_reading(pregnancy, MEDIUM_VITALS)
    return reassess_risk(pregnancy)


def recover(pregnancy):
    add_reading(pregnancy, LOW_VITALS)
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
    assert alert.level == "high"
    assert "hypertensive crisis" in alert.reasons[0].lower()


def test_a_stable_patient_raises_nothing(make_hospital, pregnancy_for):
    hospital = make_hospital("Quiet Hospital")
    pregnancy = pregnancy_for(hospital)

    add_reading(pregnancy, LOW_VITALS)
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
        Alert.objects.create(pregnancy=pregnancy, assessment=first, level="high")


# -- Worsening -----------------------------------------------------------------


def test_worsening_sharpens_the_existing_alert(make_hospital, pregnancy_for):
    hospital = make_hospital("Worsen Hospital")
    pregnancy = pregnancy_for(hospital)
    go_moderate(pregnancy)

    go_critical(pregnancy)

    alert = Alert.objects.get(pregnancy=pregnancy)
    assert alert.level == "high"
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
    make_hospital,
    pregnancy_for,
    make_staff,
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
    make_hospital,
    pregnancy_for,
    make_staff,
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
    make_hospital,
    pregnancy_for,
    make_staff,
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


def test_alerts_never_send_email_only_the_portal_notification(make_hospital, pregnancy_for, make_staff):
    """There is one Resend account shared by every hospital and no separate
    staging backend, so an email on every threshold crossing would spend the
    same quota a real emergency needs. The in-portal alert is the only
    channel — proven here by checking the one thing that must still happen:
    the alert itself, its tier, and the audit trail recording who was told."""
    hospital = make_hospital("Quiet Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doc@quiet.test")
    pregnancy = pregnancy_for(hospital, clinician=doctor)
    mail.outbox.clear()

    go_critical(pregnancy)

    assert mail.outbox == []
    alert = Alert.objects.get(pregnancy=pregnancy)
    assert alert.tier == escalation.TIER_CLINICIAN
    notified = alert.events.get(kind=AlertEvent.KIND_NOTIFIED)
    assert doctor.email in notified.detail or doctor.get_full_name() in notified.detail


# -- The API -------------------------------------------------------------------


def test_the_list_shows_live_alerts_with_the_patient_inline(
    client,
    make_hospital,
    pregnancy_for,
    auth,
):
    hospital = make_hospital("List Hospital")
    pregnancy = pregnancy_for(hospital, first_name="Zainab")
    go_critical(pregnancy)

    body = client.get(ALERTS, **auth(hospital.admin.email)).json()

    assert body["count"] == 1
    row = body["results"][0]
    assert row["patient_name"] == "Zainab Bibi"
    assert row["level"] == "high"
    assert row["reasons"]
    assert body["unacknowledged"] == 1


def test_the_list_puts_the_most_severe_first(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Order Hospital")
    mild = pregnancy_for(hospital, first_name="Mild")
    severe = pregnancy_for(hospital, first_name="Severe")
    go_moderate(mild)
    go_critical(severe)

    results = client.get(ALERTS, **auth(hospital.admin.email)).json()["results"]

    assert [r["level"] for r in results] == ["high", "medium"]


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
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
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
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
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


# -- ?assigned_to=me ------------------------------------------------------------


def test_a_provider_sees_only_the_cases_they_hold_the_provider_slot_on(
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
):
    """There is no separate "co-provider" concept any more — the provider
    slot is a single direct field, so each provider sees exactly their own
    cases and nobody else's."""
    hospital = make_hospital("Provider Alerts Hospital")
    lead = make_staff(hospital.org, settings.ROLE_PROVIDER, "lead@provideralerts.test")
    other = make_staff(hospital.org, settings.ROLE_PROVIDER, "co@provideralerts.test")
    bystander = make_staff(hospital.org, settings.ROLE_PROVIDER, "bystander@provideralerts.test")

    lead_case = pregnancy_for(hospital, first_name="Lead", clinician=lead)
    go_critical(lead_case)

    other_case = pregnancy_for(hospital, first_name="Co", clinician=other)
    go_critical(other_case)

    body = client.get(f"{ALERTS}?assigned_to=me", **auth(lead.email)).json()
    assert body["count"] == 1
    assert body["results"][0]["patient_name"] == "Lead Bibi"

    body = client.get(f"{ALERTS}?assigned_to=me", **auth(other.email)).json()
    assert body["count"] == 1
    assert body["results"][0]["patient_name"] == "Co Bibi"

    body = client.get(f"{ALERTS}?assigned_to=me", **auth(bystander.email)).json()
    assert body["count"] == 0


def test_a_nurse_sees_the_case_they_hold_the_nurse_slot_on(
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
):
    hospital = make_hospital("Nurse Alerts Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@nursealerts.test")
    pregnancy = pregnancy_for(hospital)
    pregnancy.nurse = nurse.staff
    pregnancy.save(update_fields=["nurse", "updated_at"])
    go_critical(pregnancy)

    body = client.get(f"{ALERTS}?assigned_to=me", **auth(nurse.email)).json()

    assert body["count"] == 1


def test_unassigning_a_nurse_no_longer_surfaces_the_alert(
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
):
    hospital = make_hospital("Ended Alerts Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@endedalerts.test")
    pregnancy = pregnancy_for(hospital)
    pregnancy.nurse = nurse.staff
    pregnancy.save(update_fields=["nurse", "updated_at"])
    go_critical(pregnancy)

    pregnancy.nurse = None
    pregnancy.save(update_fields=["nurse", "updated_at"])

    body = client.get(f"{ALERTS}?assigned_to=me", **auth(nurse.email)).json()

    assert body["count"] == 0


def test_hospital_admin_gets_an_honest_empty_list_for_assigned_to_me(
    client,
    make_hospital,
    pregnancy_for,
    auth,
):
    """ "My alerts" isn't a concept that applies to an admin — an honest empty
    result, not the param silently ignored and everyone's alerts returned
    under a label that would be wrong for this role."""
    hospital = make_hospital("Admin Alerts Hospital")
    go_critical(pregnancy_for(hospital))

    body = client.get(f"{ALERTS}?assigned_to=me", **auth(hospital.admin.email)).json()

    assert body["count"] == 0


def test_assigned_to_me_removed_would_leak_everyones_alerts(
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
):
    """Fault injection, matching this project's own testing discipline: prove
    the filter is actually doing something by checking what an *unfiltered*
    request to the same hospital would return, rather than trusting the
    assigned_to=me result in isolation."""
    hospital = make_hospital("Fault Alerts Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@faultalerts.test")
    mine = pregnancy_for(hospital, first_name="Mine")
    mine.nurse = nurse.staff
    mine.save(update_fields=["nurse", "updated_at"])
    go_critical(mine)
    go_critical(pregnancy_for(hospital, first_name="NotMine"))

    unfiltered = client.get(ALERTS, **auth(nurse.email)).json()
    filtered = client.get(f"{ALERTS}?assigned_to=me", **auth(nurse.email)).json()

    assert unfiltered["count"] == 2
    assert filtered["count"] == 1


# -- Tenant isolation ----------------------------------------------------------


def test_alerts_never_cross_hospitals(client, make_hospital, pregnancy_for, auth):
    alpha = make_hospital("Alpha Alerts")
    beta = make_hospital("Beta Alerts")
    go_critical(pregnancy_for(beta))

    body = client.get(ALERTS, **auth(alpha.admin.email)).json()

    assert body["count"] == 0


def test_another_hospitals_alert_cannot_be_acknowledged(
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
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
    assert alert is not None

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
    # A patient already attached to a hospital has, by construction, already
    # signed in once before (that's how a join request gets submitted) —
    # under LoginView's email-verification gate, an unverified patient can
    # never reach that state, so a fixture representing one must be verified
    # to accurately stand in for it. Not what this test is about; without it
    # the login below 403s on verification, not on the role gate being proven.
    mother.is_email_verified = True
    mother.save(update_fields=["organization", "is_email_verified", "updated_at"])

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
