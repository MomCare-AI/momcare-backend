"""The weekly care plan sweep as a Celery task.

Schedule ``care_plans.sweep`` in django-celery-beat every 15 minutes (see DEPLOY.md). Until a
worker runs, the same function runs from ``manage.py sweep_care_plans`` under cron.
"""

from celery import shared_task

from momcare_platform.modules.pregnancy.care_plans.services import process_assessment_by_id
from momcare_platform.modules.pregnancy.care_plans.sweep import run_sweep


@shared_task(name="care_plans.sweep")
def sweep_task() -> dict:
    started, retried = run_sweep()
    return {"started": started, "retried": retried}


@shared_task(name="care_plans.process_assessment", ignore_result=True)
def process_assessment_task(assessment_id: str) -> None:
    """Write (or update) the care plan a saved reading calls for -- off the request, so the
    reading saves at once. A failure is logged and left to the sweep to retry."""
    process_assessment_by_id(assessment_id)
