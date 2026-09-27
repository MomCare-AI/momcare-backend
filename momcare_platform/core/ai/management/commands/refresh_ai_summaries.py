"""The periodic safety net for AI Summaries -- catches everything the
enrollment and risk-level-change triggers don't (new notes, care-team
changes, monitoring activity), on a shorter cadence than "once a day".

Run it on a schedule, same operational pattern as escalate_alerts:

    # Linux / macOS - crontab, every 4 hours
    0 */4 * * * cd /srv/momcare && uv run python manage.py refresh_ai_summaries

    # Windows - Task Scheduler, repeat every 4 hours
    schtasks /create /tn MomCareRefreshAISummaries /sc hourly /mo 4 ^
      /tr "cmd /c cd /d D:\\path\\to\\backend && uv run python manage.py refresh_ai_summaries"

Safe to run as often as you like: a summary generated moments ago is simply
skipped until it goes stale again.
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from momcare_platform.core.ai.services import generate_patient_summary
from momcare_platform.core.common.rls import bypass_rls
from momcare_platform.core.patients.models import Patient


class Command(BaseCommand):
    help = "Refresh any active patient's AI Summary that is missing or older than the configured staleness window."

    def handle(self, *args, **options):
        cutoff = timezone.now() - timezone.timedelta(hours=settings.MOMCARE_AI_SUMMARY_REFRESH_HOURS)

        # This command sweeps every hospital's active patients in one pass,
        # by design -- same sanctioned bypass escalate_alerts already uses.
        with bypass_rls():
            patients = Patient.objects.filter(is_active=True).filter(
                Q(ai_summary__isnull=True) | Q(ai_summary__generated_at__lt=cutoff),
            )
            refreshed = 0
            for patient in patients:
                generate_patient_summary(patient)
                refreshed += 1

        if refreshed:
            self.stdout.write(self.style.SUCCESS(f"Refreshed {refreshed} AI summary(ies)."))
        else:
            self.stdout.write("No AI summary was due for a refresh.")
