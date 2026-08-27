"""Climb any alert nobody has answered.

Raising an alert happens inside the request that recorded the reading, so it is
immediate. **Escalation is the part that needs a clock**, and this command is
that clock.

Run it on a schedule. Every minute is appropriate: the tightest interval in the
policy is five minutes for a critical alert, and a sweep that runs less often
than the shortest deadline turns a five-minute promise into a lie.

    # Linux / macOS - crontab
    * * * * * cd /srv/momcare && uv run python manage.py escalate_alerts

    # Windows - Task Scheduler, repeat every 1 minute
    schtasks /create /tn MomCareEscalate /sc minute /mo 1 ^
      /tr "cmd /c cd /d D:\\path\\to\\backend && uv run python manage.py escalate_alerts"

Nothing schedules this automatically. If it is not scheduled, alerts are still
raised and still notify the assigned clinician - but they never climb, and an
unanswered critical alert stays unanswered. That is a deployment step, not an
optional extra.

Safe to run as often as you like: the target tier is computed from the clock,
so running twice in a second does nothing the second time, and a sweep that
ran late lands on the correct rung rather than stepping up once per missed run.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from momcare_platform.core.alerts.services import escalate_due_alerts
from momcare_platform.core.common.rls import bypass_rls


class Command(BaseCommand):
    help = "Escalate alerts that have gone unanswered past their tier deadline."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would escalate without notifying anyone or writing events.",
        )

    def handle(self, *args, **options):
        now = timezone.now()

        # This command sweeps every hospital's open alerts in one pass, by
        # design - escalation would not work otherwise. Explicit bypass is the
        # one sanctioned way past row-level security; see core/common/rls.py.
        with bypass_rls():
            if options["dry_run"]:
                self._report(now)
                return

            moved = escalate_due_alerts(now=now)

            if moved:
                self.stdout.write(self.style.WARNING(f"Escalated {moved} alert(s)."))
            else:
                self.stdout.write("No alert was due to escalate.")

    def _report(self, now):
        """Show the ladder without touching it - useful when tuning the policy."""
        from momcare_platform.core.alerts import escalation  # noqa: PLC0415
        from momcare_platform.core.alerts.models import Alert  # noqa: PLC0415

        open_alerts = Alert.objects.filter(status=Alert.STATUS_OPEN).select_related(
            "pregnancy__patient",
        )
        if not open_alerts:
            self.stdout.write("No open alerts.")
            return

        for alert in open_alerts:
            target = escalation.due_tier(alert.level, alert.raised_at, now)
            waited = int((now - alert.raised_at).total_seconds() // 60)
            verdict = (
                f"would escalate to {escalation.tier_label(target).lower()}"
                if target > alert.tier
                else "no change"
            )
            self.stdout.write(
                f"{alert.pregnancy.patient.full_name:20} {alert.level:9} "
                f"waited {waited:4}m  tier {alert.tier} -> {verdict}",
            )
