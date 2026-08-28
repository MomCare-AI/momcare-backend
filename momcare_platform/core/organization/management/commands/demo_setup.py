"""Prepare the development database for a live demonstration.

Two jobs, both of them chores that otherwise get done by hand five minutes
before a meeting:

1. Put a **known password** on every account, so a walkthrough can sign in as a
   doctor, a second hospital, or the platform admin without guessing.
2. Optionally **rewind the alert state** for one patient, so the raise →
   escalate → acknowledge sequence can be shown again from the start.

Refuses to run when ``DEBUG`` is off. Resetting everybody's password is exactly
the right thing on a demo laptop and exactly the wrong thing anywhere else, and
that difference should not depend on somebody reading this docstring.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from momcare_platform.core.common.rls import bypass_rls

DEMO_PASSWORD = "MomCare!Demo2026"


class Command(BaseCommand):
    help = "Set a known password on all accounts and optionally rewind the demo alert state."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            default=DEMO_PASSWORD,
            help=f"Password to set on every account (default: {DEMO_PASSWORD}).",
        )
        parser.add_argument(
            "--include-admins",
            action="store_true",
            help=(
                "Also reset platform administrators and superusers. Off by default: "
                "those are real people's own accounts, not demo props."
            ),
        )
        parser.add_argument(
            "--reset-alerts",
            action="store_true",
            help=(
                "Clear alerts and risk assessments so the alert flow can be demonstrated "
                "from the beginning. Readings and patient records are never touched."
            ),
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "demo_setup refuses to run with DEBUG off. It rewrites every password, "
                "which is only ever appropriate on a development machine.",
            )

        password = options["password"]

        # Only ever reachable with DEBUG on (checked above), so this never
        # runs against production - but it reads and writes across every
        # hospital by design (that is the whole point of a demo cast list),
        # which needs the same bypass any other cross-tenant management
        # command already uses, for whichever database DEBUG happens to be
        # pointed at (a local box today; a DEBUG-on staging environment with
        # real RLS enforcement is exactly the situation this exists for).
        with bypass_rls():
            if options["reset_alerts"]:
                self._reset_alerts()

            self._set_passwords(password, include_admins=options["include_admins"])
            self._report(password, include_admins=options["include_admins"])

    # -- Steps ----------------------------------------------------------------

    @transaction.atomic
    def _reset_alerts(self):
        """Rewind the judgement layer only.

        Readings, patients, pregnancies and consent are left alone — they are
        the clinical record, and a demo helper has no business deleting those.
        Assessments and alerts are derived from readings, so removing them
        costs nothing: the next re-score rebuilds both.
        """
        from momcare_platform.core.alerts.models import Alert  # noqa: PLC0415
        from momcare_platform.core.monitoring.models import RiskAssessment  # noqa: PLC0415

        alerts = Alert.objects.count()
        assessments = RiskAssessment.objects.count()
        Alert.objects.all().delete()
        RiskAssessment.objects.all().delete()

        self.stdout.write(
            self.style.WARNING(
                f"Rewound {alerts} alert(s) and {assessments} assessment(s). "
                "Readings and patient records untouched.",
            ),
        )

    def _set_passwords(self, password: str, include_admins: bool):
        """Reset the demo cast, and leave real accounts alone.

        Platform administrators and superusers are skipped unless explicitly
        asked for. Those belong to actual people who chose their own password
        and will not expect it to change because somebody prepared a demo.
        """
        from momcare_platform.core.users.models import User  # noqa: PLC0415

        users = User.objects.all()
        skipped = 0

        if not include_admins:
            protected = User.objects.filter(is_superuser=True) | User.objects.filter(
                role__code=settings.ROLE_PLATFORM_ADMIN,
            )
            protected_ids = set(protected.values_list("pk", flat=True))
            skipped = len(protected_ids)
            users = users.exclude(pk__in=protected_ids)

        changed = 0
        for user in users:
            user.set_password(password)
            user.save(update_fields=["password"])
            changed += 1

        self.stdout.write(self.style.SUCCESS(f"Set the demo password on {changed} account(s)."))
        if skipped:
            self.stdout.write(
                f"Left {skipped} platform/superuser account(s) untouched. "
                "Pass --include-admins to reset those too.",
            )

    def _report(self, password: str, include_admins: bool = True):
        """Print the cast list, so the walkthrough has its sign-ins to hand."""
        from momcare_platform.core.organization.models import Organization  # noqa: PLC0415
        from momcare_platform.core.users.models import User  # noqa: PLC0415

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"Password for every account: {password}"))
        self.stdout.write("")

        platform = User.objects.filter(organization__isnull=True)
        if platform:
            suffix = "" if include_admins else "  (password NOT changed)"
            self.stdout.write(self.style.MIGRATE_HEADING(f"Platform administrators{suffix}"))
            for user in platform:
                self.stdout.write(f"  {user.email}")
            self.stdout.write("")

        for org in Organization.objects.order_by("status", "name"):
            users = User.objects.filter(organization=org).select_related("role")
            header = f"{org.name}  [{org.status}]"
            self.stdout.write(self.style.MIGRATE_HEADING(header))
            if not users:
                self.stdout.write("  (no accounts)")
            for user in users:
                role = user.role.code if user.role else "-"
                self.stdout.write(f"  {user.email:30} {role}")
            self.stdout.write("")
