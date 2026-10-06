"""Run the weekly care plan sweep (see ``care_plans.sweep`` for what it does).

# Linux / macOS - crontab, every 15 minutes
*/15 * * * * cd /srv/momcare && uv run python manage.py sweep_care_plans
"""

from django.core.management.base import BaseCommand

from momcare_platform.modules.pregnancy.care_plans.sweep import run_sweep


class Command(BaseCommand):
    help = "Write new pregnancy weeks' care plans (at 12 am local time) and retry any week stuck on the fallback."

    def handle(self, *args, **options):
        started, retried = run_sweep()
        if started or retried:
            self.stdout.write(
                self.style.SUCCESS(f"Started {started} week plan(s); retried {retried} stuck on the fallback.")
            )
        else:
            self.stdout.write("No care plan needed evaluating.")
