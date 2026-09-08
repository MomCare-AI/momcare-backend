"""Populate a deployment with a fictional hospital that can be demonstrated.

A fresh production database contains six role rows and nothing else. Without
this, a successful deploy produces a working link to an empty system.

**Safe to run repeatedly, and meant to be.** People are created once and then
left alone; readings are appended, never deleted, so the immutability rule this
system enforces everywhere else is not broken here for convenience. Re-running
refreshes the clinical timeline without disturbing the patients.

That matters because of a trap in the risk engine: ``STALE_AFTER`` is twelve
hours, so readings written on Monday make every patient report as *not
currently being monitored* by Tuesday. Seeded data has to be generated relative
to now, and refreshed before each demonstration - so this is a demo-refresh
command, not a one-time setup step.

    python manage.py seed_demo

Set ``DJANGO_DEMO_PASSWORD`` first. The command never invents a password and
never prints one: on most platforms stdout goes to deploy logs that persist and
are readable by anyone with dashboard access.
"""

from __future__ import annotations

import os
import random
from datetime import timedelta
from decimal import Decimal
from typing import cast

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from momcare_platform.core.common.rls import bypass_rls

# Named so nobody can mistake this for a real hospital — the people would not
# otherwise be marked as fictional at all.
DEMO_ORG_NAME = "MomCare Demonstration Hospital"

# Loosely realistic resting ranges for pregnancy, used only to give the demo
# hospital a believable clinical history. Not clinical reference values, and
# deliberately not a reusable "simulate" capability reachable from the real
# product — see MEMORY.md on why the old SimulateReadingsView was removed.
# Every row this produces is tagged source=SOURCE_MANUAL: fictional as data
# goes, it is not a third kind of provenance the schema needs to represent.
# Hemoglobin is excluded: it comes from a monthly lab report, not a band, so
# generated history never invents one. Temperature in Fahrenheit, matching
# VitalReading.body_temp_f.
_NORMAL_RANGES = {
    "systolic_bp": (105, 128),
    "diastolic_bp": (65, 82),
    "heart_rate": (72, 96),
    "body_temp_f": (97.5, 99.0),
    "blood_glucose": (85, 110),
    "stress_score": (1, 4),
    "phys_activity_score": (4, 8),
}

_ELEVATED_RANGES = {
    "systolic_bp": (142, 165),
    "diastolic_bp": (92, 108),
    "heart_rate": (104, 124),
    "body_temp_f": (100.2, 101.5),
    "blood_glucose": (130, 180),
    "stress_score": (6, 9),
    "phys_activity_score": (1, 3),
}


def _dec(value: float, places: str = "0.01") -> Decimal:
    return Decimal(str(round(value, 2))).quantize(Decimal(places))


def _demo_vitals(elevated: bool, *, age: int | None) -> dict:
    ranges = _ELEVATED_RANGES if elevated else _NORMAL_RANGES
    return {
        "age": age,
        **{field: _dec(random.uniform(*band)) for field, band in ranges.items()},
    }

DEMO_STAFF = [
    ("admin@demo.momcare.solutions", "Demo", "Administrator", "ROLE_HOSPITAL_ADMIN"),
    ("doctor@demo.momcare.solutions", "Demo", "Doctor", "ROLE_PROVIDER"),
    ("nurse@demo.momcare.solutions", "Demo", "Nurse", "ROLE_NURSE"),
]


def redirect_to_inbox(address: str, contact: str) -> str:
    """Rewrite a demo address so its mail arrives in one real inbox.

    ``doctor@demo.momcare.solutions`` with a contact of ``you@gmail.com``
    becomes ``you+doctor@gmail.com``. Gmail and most providers ignore everything
    from the ``+`` to the ``@`` when delivering, so three staff with three
    distinct sign-in identities all reach the same person.

    This exists so invitations, alerts and password resets can be *read* during
    a walkthrough rather than assumed to have been sent. Nothing else about the
    accounts changes, and without ``--contact-email`` the addresses are the
    fictional ones and no real mailbox is involved.
    """
    tag = address.split("@", 1)[0]
    local, _, domain = contact.partition("@")
    # A contact that is already tagged keeps its own tag rather than gaining a
    # second one, which most providers would reject.
    local = local.split("+", 1)[0]
    return f"{local}+{tag}@{domain}"

# Obviously fictional. The third is deliberately left without a clinician so the
# "no clinician assigned" warning has something to show.
DEMO_PATIENTS = [
    {"first_name": "Demo", "last_name": "PatientOne", "weeks": 28, "state": "elevated", "assign": True},
    {"first_name": "Demo", "last_name": "PatientTwo", "weeks": 34, "state": "normal", "assign": True},
    {"first_name": "Demo", "last_name": "PatientThree", "weeks": 19, "state": "normal", "assign": False},
]


class Command(BaseCommand):
    help = "Create or refresh the fictional demonstration hospital."

    def add_arguments(self, parser):
        parser.add_argument(
            "--contact-email",
            default="",
            help=(
                "Deliver every demo account's mail to this real address, using "
                "plus-addressing: --contact-email you@gmail.com makes the doctor "
                "you+doctor@gmail.com. Use it to read invitations and alerts "
                "during a walkthrough instead of assuming they were sent."
            ),
        )
        parser.add_argument(
            "--hours",
            type=int,
            default=24,
            help="How much recent reading history to generate (default: 24).",
        )

    def handle(self, *args, **options):
        password = os.environ.get("DJANGO_DEMO_PASSWORD", "").strip()
        if not password:
            raise CommandError(
                "Set DJANGO_DEMO_PASSWORD before running this. The command will not "
                "invent a password, because printing one would put it in the deploy log.",
            )

        contact = options["contact_email"].strip()
        if contact and "@" not in contact:
            raise CommandError(f"--contact-email is not an address: {contact!r}")

        # _organization() must see every hospital to refuse running beside a
        # real one; the rest creates and reads data with no request and no
        # per-hospital scope to set. Explicit bypass, not implicit - see
        # core/common/rls.py.
        with bypass_rls():
            org = self._organization()
            staff = self._staff(org, password, contact)
            patients = self._patients(org, staff)
            self._refresh_clinical_data(patients, hours=options["hours"])
            self._report(org, staff, patients)

    # -- Hospital -------------------------------------------------------------

    def _organization(self):
        """Create the demo hospital, or return it — and refuse to run beside real ones."""
        from momcare_platform.core.organization.models import Organization  # noqa: PLC0415

        others = Organization.objects.exclude(name=DEMO_ORG_NAME)
        if others.exists():
            raise CommandError(
                "This database already contains other organizations:\n  "
                + "\n  ".join(others.values_list("name", flat=True))
                + "\n\nseed_demo only ever populates an empty or demo-only system, so it "
                "cannot write fictional patients into a deployment holding real ones.",
            )

        org = Organization.objects.filter(name=DEMO_ORG_NAME).first()
        if org:
            self.stdout.write(f"Hospital already present: {org.name}")
            return org

        org = Organization.objects.create(
            name=DEMO_ORG_NAME,
            email="demo@momcare.solutions",
            phone="0000000000",
            license_no="DEMO-0000",
            city="Lahore",
            country="Pakistan",
        )
        # Approve without notifying: there is no real applicant to email, and a
        # failed send would only add noise to the deploy log.
        org.set_review_status(
            Organization.STATUS_APPROVED,
            note="Fictional hospital created by seed_demo for demonstration purposes.",
            notify=False,
        )
        self.stdout.write(self.style.SUCCESS(f"Created hospital: {org.name}"))
        return org

    # -- People ---------------------------------------------------------------

    def _staff(self, org, password: str, contact: str = "") -> dict:
        """Create the three demo accounts, or return the existing ones.

        ``contact`` redirects their mail to one real inbox - see
        ``redirect_to_inbox``. It changes the sign-in address, so passing it
        creates a second set of accounts rather than editing the first.
        """
        from momcare_platform.core.staff.models import Staff  # noqa: PLC0415
        from momcare_platform.core.staff.services import _next_employee_id  # noqa: PLC0415
        from momcare_platform.core.users.models import Role, User  # noqa: PLC0415

        people = {}
        for email, first, last, role_setting in DEMO_STAFF:
            if contact:
                email = redirect_to_inbox(email, contact)
            role_code = getattr(settings, role_setting)
            user = User.objects.filter(email=email).first()

            if user is None:
                user = User.objects.create_user(
                    email=email,
                    password=password,
                    first_name=first,
                    last_name=last,
                    role=Role.objects.get(code=role_code),
                )
                user.organization = org
                user.save(update_fields=["organization", "updated_at"])
                Staff.objects.create(user=user, employee_id=_next_employee_id(org))
                self.stdout.write(f"  created {role_code:16} {email}")
            else:
                # Keep the password in step with the environment variable, so
                # rotating it is a matter of re-running rather than editing rows.
                user.set_password(password)
                user.save(update_fields=["password"])
                self.stdout.write(f"  present {role_code:16} {email}")

            people[role_code] = user
        return people

    def _patients(self, org, staff: dict) -> list:
        """Enrol the demo patients, or return those already here."""
        from momcare_platform.core.patients.models import Consent, Patient  # noqa: PLC0415
        from momcare_platform.core.patients.services import enrol_patient  # noqa: PLC0415

        admin = staff[settings.ROLE_HOSPITAL_ADMIN]
        doctor = staff[settings.ROLE_PROVIDER]

        enrolled = []
        for spec in DEMO_PATIENTS:
            existing = Patient.objects.filter(
                first_name=spec["first_name"],
                last_name=spec["last_name"],
                location__organization=org,
            ).first()

            if existing:
                self.stdout.write(f"  present patient  {existing.full_name}")
                enrolled.append((existing, spec))
                continue

            pregnancy_data = {
                "lmp": timezone.now().date() - timedelta(weeks=cast(int, spec["weeks"])),
            }
            if spec["assign"]:
                pregnancy_data["assigned_staff"] = doctor.staff

            patient = enrol_patient(
                organization=org,
                recorded_by=admin,
                patient_data={
                    "first_name": spec["first_name"],
                    "last_name": spec["last_name"],
                    "phone": "0000000000",
                },
                pregnancy_data=pregnancy_data,
                consent={
                    "status": Consent.STATUS_GRANTED,
                    "version": "demo-v1",
                    "method": Consent.METHOD_IN_PERSON,
                },
            )
            self.stdout.write(f"  created patient  {patient.full_name}")
            enrolled.append((patient, spec))

        return enrolled

    # -- Clinical timeline ----------------------------------------------------

    @transaction.atomic
    def _refresh_clinical_data(self, patients: list, *, hours: int):
        """Append a fresh window of readings and re-score.

        Nothing is deleted. Older readings stay where they are — they are
        observations of moments that did happen, and this system does not edit
        or remove those anywhere else. The engine reads the *latest* value of
        each measurement, so appending recent data is enough to make the demo
        current, and it leaves a believable history behind it.
        """
        from momcare_platform.core.monitoring.models import VitalReading  # noqa: PLC0415
        from momcare_platform.core.monitoring.services import reassess_risk  # noqa: PLC0415

        for patient, spec in patients:
            pregnancy = patient.current_pregnancy
            if pregnancy is None:
                continue

            now = timezone.now()
            start = now - timedelta(hours=hours)
            dob = patient.date_of_birth
            age = (now.date() - dob).days // 365 if dob else None
            elevated = spec["state"] == "elevated"

            readings = []
            moment = start
            while moment <= now:
                readings.append(
                    VitalReading(
                        pregnancy=pregnancy,
                        recorded_at=moment,
                        source=VitalReading.SOURCE_MANUAL,
                        **_demo_vitals(elevated, age=age),
                    ),
                )
                moment += timedelta(hours=1)
            VitalReading.objects.bulk_create(readings)

            assessment = reassess_risk(pregnancy)
            level = assessment.final_risk_level if assessment else "unchanged"
            self.stdout.write(f"  {patient.full_name:22} +{len(readings):4} readings  -> {level}")

    # -- Output ---------------------------------------------------------------

    def _report(self, org, staff: dict, patients: list):
        from momcare_platform.core.alerts.models import Alert  # noqa: PLC0415

        live = Alert.objects.filter(
            pregnancy__patient__location__organization=org,
            status__in=Alert.LIVE_STATUSES,
        ).count()

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Demonstration data is ready."))
        self.stdout.write(f"  hospital     {org.name} [{org.status}]")
        self.stdout.write(f"  accounts     {', '.join(u.email for u in staff.values())}")
        self.stdout.write(f"  patients     {len(patients)}")
        self.stdout.write(f"  live alerts  {live}")
        self.stdout.write("")
        self.stdout.write(
            "Password is whatever DJANGO_DEMO_PASSWORD is set to; it is not printed here.",
        )
        self.stdout.write(
            "Re-run before each demonstration: readings older than 12 hours make every "
            "patient report as not currently being monitored.",
        )
