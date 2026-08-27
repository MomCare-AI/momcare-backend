"""Who may end an escalation.

The three-tier ladder climbs *towards* the hospital administrator: an alert
reaches them precisely because nobody nearer the patient answered it. So an
administrator who could acknowledge would be able to stop the ladder at the
moment it arrived at them, with no clinician having looked at the patient, and
the record would say the alert was answered.

These tests are about that boundary. A hospital administrator runs the
hospital's presence on the platform - staff, licence, oversight - and is not
required to have any clinical training.
"""

import json
from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from momcare_platform.core.alerts.models import Alert
from momcare_platform.core.monitoring.models import VitalReading
from momcare_platform.core.monitoring.services import reassess_risk
from momcare_platform.core.patients.models import Consent
from momcare_platform.core.patients.services import enrol_patient

pytestmark = pytest.mark.django_db

CLINICAL_ROLES = [settings.ROLE_PROVIDER, settings.ROLE_NURSE, settings.ROLE_CARE_MANAGER]


@pytest.fixture
def live_alert(db):
    """An open alert at a hospital, raised the way a real one is.

    Built by recording a dangerous reading rather than by creating the row
    directly, so these tests exercise the same object the system produces.
    """

    def _make(hospital, *, first_name="Ayesha"):
        patient = enrol_patient(
            organization=hospital.org,
            recorded_by=hospital.admin,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
            pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=28)},
            consent={"status": Consent.STATUS_GRANTED},
        )
        pregnancy = patient.current_pregnancy
        VitalReading.objects.create(
            pregnancy=pregnancy,
            reading_type=VitalReading.TYPE_BLOOD_PRESSURE,
            value=168,
            value_secondary=112,
            recorded_at=timezone.now(),
            source=VitalReading.SOURCE_MANUAL,
        )
        reassess_risk(pregnancy)
        return Alert.objects.get(pregnancy=pregnancy)

    return _make


def acknowledge(client, alert, headers):
    return client.post(f"/api/alerts/{alert.id}/acknowledge/", **headers)


def resolve(client, alert, headers, resolution="recovered"):
    return client.post(
        f"/api/alerts/{alert.id}/resolve/",
        data=json.dumps({"resolution": resolution}),
        content_type="application/json",
        **headers,
    )


# -- The administrator may look, and may not judge -----------------------------


def test_a_hospital_admin_cannot_acknowledge(client, make_hospital, auth, live_alert):
    """The ladder must not be stoppable by the person it is escalating to."""
    hospital = make_hospital("Oversight Hospital")
    alert = live_alert(hospital)

    response = acknowledge(client, alert, auth(hospital.admin.email))

    assert response.status_code == 403, response.content
    alert.refresh_from_db()
    assert alert.status == Alert.STATUS_OPEN
    assert alert.acknowledged_at is None


def test_a_hospital_admin_cannot_resolve(client, make_hospital, auth, live_alert):
    """"Recovered" and "handled" are statements about a patient.

    An alert nobody answered is also evidence about how this hospital is
    covered. An administrator able to close it would be tidying away the one
    signal that should prompt them to fix the rota.
    """
    hospital = make_hospital("Tidy Hospital")
    alert = live_alert(hospital)

    response = resolve(client, alert, auth(hospital.admin.email))

    assert response.status_code == 403, response.content
    alert.refresh_from_db()
    assert alert.status == Alert.STATUS_OPEN
    assert alert.resolved_at is None


def test_a_hospital_admin_can_still_read_every_alert(client, make_hospital, auth, live_alert):
    """Oversight is the administrator's job. Blocking the clinical verbs must
    not blind them to what the hospital is doing."""
    hospital = make_hospital("Reading Hospital")
    alert = live_alert(hospital)
    headers = auth(hospital.admin.email)

    listed = client.get("/api/alerts/", **headers)
    assert listed.status_code == 200
    assert any(row["id"] == str(alert.id) for row in listed.json()["results"])

    detail = client.get(f"/api/alerts/{alert.id}/", **headers)
    assert detail.status_code == 200
    assert "events" in detail.json()


# -- Clinicians may -----------------------------------------------------------


@pytest.mark.parametrize("role_code", CLINICAL_ROLES)
def test_every_clinical_role_can_acknowledge(
    client, make_hospital, make_staff, auth, live_alert, role_code
):
    hospital = make_hospital(f"Ack {role_code} Hospital")
    member = make_staff(hospital.org, role_code, email=f"{role_code}@ack.test")
    alert = live_alert(hospital)

    response = acknowledge(client, alert, auth(member.email))

    assert response.status_code == 200, response.content
    alert.refresh_from_db()
    assert alert.status == Alert.STATUS_ACKNOWLEDGED
    assert alert.acknowledged_by_id == member.id


@pytest.mark.parametrize("role_code", CLINICAL_ROLES)
def test_every_clinical_role_can_resolve(
    client, make_hospital, make_staff, auth, live_alert, role_code
):
    hospital = make_hospital(f"Res {role_code} Hospital")
    member = make_staff(hospital.org, role_code, email=f"{role_code}@res.test")
    alert = live_alert(hospital)

    response = resolve(client, alert, auth(member.email))

    assert response.status_code == 200, response.content
    alert.refresh_from_db()
    assert alert.status == Alert.STATUS_RESOLVED


# -- The tenant boundary still holds ------------------------------------------


def test_a_clinician_cannot_reach_another_hospital_alert(
    client, make_hospital, make_staff, auth, live_alert
):
    """Being a clinician is authority over your own patients, not everyone's.

    404 rather than 403: a 403 would confirm the alert exists somewhere else.
    """
    ours = make_hospital("Ours Hospital")
    theirs = make_hospital("Theirs Hospital")
    outsider = make_staff(ours.org, settings.ROLE_PROVIDER, email="doctor@ours.test")
    alert = live_alert(theirs)

    response = acknowledge(client, alert, auth(outsider.email))

    assert response.status_code == 404, response.content
    alert.refresh_from_db()
    assert alert.status == Alert.STATUS_OPEN
