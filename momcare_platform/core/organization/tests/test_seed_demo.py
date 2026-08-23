"""The demo seeder.

Two properties matter more than the rest: it must be safe to run twice, and it
must refuse to run anywhere near a real hospital. Everything else is
convenience; those two are what stop it causing damage on a live deployment.
"""

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from momcare_platform.core.alerts.models import Alert
from momcare_platform.core.monitoring.models import VitalReading
from momcare_platform.core.organization.management.commands.seed_demo import DEMO_ORG_NAME
from momcare_platform.core.organization.models import Organization
from momcare_platform.core.patients.models import Patient
from momcare_platform.core.users.models import User

pytestmark = pytest.mark.django_db

PASSWORD = "SeedDemo!Pass2026"


@pytest.fixture(autouse=True)
def _demo_password(monkeypatch):
    monkeypatch.setenv("DJANGO_DEMO_PASSWORD", PASSWORD)


def seed(**kwargs):
    call_command("seed_demo", **kwargs)


# ── It produces a usable system ──────────────────────────────────────────────


def test_it_builds_a_hospital_that_can_be_signed_into(client):
    seed(hours=6)

    org = Organization.objects.get(name=DEMO_ORG_NAME)
    assert org.status == Organization.STATUS_APPROVED, "a pending hospital cannot sign in"

    response = client.post(
        "/api/auth/login/",
        data={"email": "admin@demo.momcare.solutions", "password": PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200


def test_it_enrols_patients_with_readings_and_a_live_alert(client):
    """An empty system is a failed demo, so this asserts the whole chain."""
    seed(hours=6)

    assert Patient.objects.count() == 3
    assert VitalReading.objects.exists()
    assert Alert.objects.filter(status__in=Alert.LIVE_STATUSES).exists(), (
        "no alert was raised, so the escalation demo would have nothing to show"
    )


def test_readings_are_recent_enough_to_read_as_monitored():
    """The trap this command exists to avoid.

    STALE_AFTER is twelve hours. Data written with fixed timestamps makes every
    patient report as "not currently being monitored" a day later, turning the
    intended Critical demonstration into an unremarkable Moderate one.
    """
    seed(hours=6)

    newest = VitalReading.objects.order_by("-recorded_at").first()
    age = timezone.now() - newest.recorded_at
    assert age.total_seconds() < 3600, "the newest reading should be within the last hour"


def test_one_patient_is_left_without_a_clinician():
    """The unassigned-patient warning needs a case to display."""
    seed(hours=6)

    unassigned = [
        p for p in Patient.objects.all()
        if p.current_pregnancy and p.current_pregnancy.assigned_staff is None
    ]
    assert unassigned, "every patient has a clinician, so that warning cannot be demonstrated"


# ── It is safe to run twice ──────────────────────────────────────────────────


def test_running_twice_does_not_duplicate_anyone():
    seed(hours=6)
    seed(hours=6)

    assert Organization.objects.filter(name=DEMO_ORG_NAME).count() == 1
    assert Patient.objects.count() == 3
    assert User.objects.filter(email="doctor@demo.momcare.solutions").count() == 1


def test_running_twice_adds_readings_rather_than_deleting_them():
    """Observations are immutable everywhere else in this system, and a
    convenience command is not a reason to make an exception."""
    seed(hours=6)
    first = VitalReading.objects.count()
    oldest_before = VitalReading.objects.order_by("recorded_at").first().id

    seed(hours=6)

    assert VitalReading.objects.count() > first, "the refresh added nothing"
    assert VitalReading.objects.filter(id=oldest_before).exists(), (
        "an earlier reading was deleted; readings must only ever be appended"
    )


def test_a_second_run_refreshes_the_password_from_the_environment(client, monkeypatch):
    """So rotating the demo password is a re-run, not a database edit."""
    seed(hours=6)
    monkeypatch.setenv("DJANGO_DEMO_PASSWORD", "Rotated!Pass2026")

    seed(hours=6)

    response = client.post(
        "/api/auth/login/",
        data={"email": "admin@demo.momcare.solutions", "password": "Rotated!Pass2026"},
        content_type="application/json",
    )
    assert response.status_code == 200


# ── It refuses to endanger a real deployment ─────────────────────────────────


def test_it_refuses_to_run_beside_a_real_hospital(make_hospital):
    """The guard that matters.

    demo_setup is protected by refusing when DEBUG is off. This command has to
    run with DEBUG off — that is its whole purpose — so it inherits no such
    protection and needs its own.
    """
    make_hospital("Nur Care Maternity")

    with pytest.raises(CommandError, match="other organizations"):
        seed(hours=6)

    assert not Organization.objects.filter(name=DEMO_ORG_NAME).exists()
    assert not Patient.objects.exists(), "it wrote fictional patients despite refusing"


def test_it_refuses_without_a_password_in_the_environment(monkeypatch):
    """It must never invent one: printing a generated password would place it in
    the deploy log, which persists and is readable from the platform dashboard."""
    monkeypatch.delenv("DJANGO_DEMO_PASSWORD", raising=False)

    with pytest.raises(CommandError, match="DJANGO_DEMO_PASSWORD"):
        seed(hours=6)

    assert not Organization.objects.exists()


def test_the_demo_hospital_is_named_so_it_cannot_be_mistaken_for_real():
    """Readings are labelled simulated, but nothing would otherwise mark the
    people as fictional on a publicly reachable deployment."""
    seed(hours=6)

    org = Organization.objects.get(name=DEMO_ORG_NAME)
    assert "demonstration" in org.name.lower()
    for patient in Patient.objects.all():
        assert patient.first_name.lower().startswith("demo")


def test_seeded_readings_are_labelled_as_simulated():
    seed(hours=6)

    assert not VitalReading.objects.exclude(source=VitalReading.SOURCE_SIMULATED).exists(), (
        "generated data must never be indistinguishable from a measurement"
    )


def test_it_uses_the_configured_role_codes(monkeypatch):
    """Role codes come from settings everywhere else; this must not hardcode them."""
    seed(hours=6)

    doctor = User.objects.get(email="doctor@demo.momcare.solutions")
    assert doctor.role.code == settings.ROLE_PROVIDER
