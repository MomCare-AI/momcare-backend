"""Run every scheduled AI job in one go: the weekly care plan sweep, then the AI summary refresh.

One Railway cron service (``ai-summary-sweep``, every 15 minutes) runs this single command:

    python manage.py run_ai_sweeps

Each job behaves exactly as when it is run on its own (``sweep_care_plans`` and
``refresh_ai_summaries`` stay available separately) and each is safe to run often: a week that
already has a plan, or an AI summary newer than ``MOMCARE_AI_SUMMARY_REFRESH_HOURS`` (default 4),
is left alone, so running both every 15 minutes writes each summary about every 4 hours as before.

The jobs are independent: if one raises, the other still runs, and the command then exits with an
error so the failure shows up in the cron run's status instead of being lost.
"""

import logging

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)

# By name, not by import: the summary refresh lives in ``core`` and ``core`` must not be imported
# from here in the other direction -- and a name keeps each job's own command exactly as it is.
JOBS = ("sweep_care_plans", "refresh_ai_summaries")


class Command(BaseCommand):
    help = "Run the care plan sweep and the AI summary refresh (each independent of the other's failure)."

    def handle(self, *args, **options):
        failed = []
        for name in JOBS:
            self.stdout.write(f"== {name}")
            try:
                call_command(name, stdout=self.stdout, stderr=self.stderr)
            except Exception:
                logger.exception("scheduled job %s failed", name)
                self.stderr.write(self.style.ERROR(f"{name} failed (see the log above)."))
                failed.append(name)
        if failed:
            raise CommandError("These scheduled jobs failed: " + ", ".join(failed))
        self.stdout.write(self.style.SUCCESS("All scheduled AI jobs finished."))
