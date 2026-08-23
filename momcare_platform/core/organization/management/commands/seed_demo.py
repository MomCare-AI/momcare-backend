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
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

# Named so nobody can mistake this for a real hospital. The readings carry a
# "simulated" source and show a tag in the interface; the people would not
# otherwise be marked as fictional at all.
DEMO_ORG_NAME = "MomCare Demonstration Hospital"

DEMO_STAFF = [
    ("admin@demo.momcare.solutions", "Demo", "Administrator", "ROLE_HOSPITAL_ADMIN"),
    ("doctor@demo.momcare.solutions", "Demo", "Doctor", "ROLE_PROVIDER"),
    ("nurse@demo.momcare.solutions", "Demo", "Nurse", "ROLE_NURSE"),
]

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

        org = self._organization()
        staff = self._staff(org, password)
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

    def _staff(self, org, password: str) -> dict:
        """Create the three demo accounts, or return the existing ones."""
        from momcare_platform.core.staff.models import Staff  # noqa: PLC0415
        from momcare_platform.core.staff.services import _next_employee_id  # noqa: PLC0415
        from momcare_platform.core.users.models import Role, User  # noqa: PLC0415

        people = {}
        for email, first, last, role_setting in DEMO_STAFF:
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
                "lmp": timezone.now().date() - timedelta(weeks=spec["weeks"]),
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
        from momcare_platform.core.monitoring.services import (  # noqa: PLC0415
            reassess_risk,
            simulate_readings,
        )

        for patient, spec in patients:
            pregnancy = patient.current_pregnancy
            if pregnancy is None:
                continue

            created = simulate_readings(
                pregnancy=pregnancy,
                hours=hours,
                elevated=spec["state"] == "elevated",
            )
            assessment = reassess_risk(pregnancy)
            level = assessment.level if assessment else "unchanged"
            self.stdout.write(f"  {patient.full_name:22} +{len(created):4} readings  -> {level}")

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
