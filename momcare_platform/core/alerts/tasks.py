"""The escalation sweep as a Celery task.

The project already carries a configured Celery app, so this is the production
path once a broker and a worker are running: schedule ``escalate-alerts`` in
django-celery-beat at one-minute intervals and remove the cron entry.

Until a worker runs, this task is never executed and escalation comes from the
``escalate_alerts`` management command instead. Both call the same function, so
there is one behaviour to reason about and one place to fix.
"""

from celery import shared_task

from momcare_platform.core.alerts.services import escalate_due_alerts


@shared_task(name="alerts.escalate_due_alerts")
def escalate_due_alerts_task() -> int:
    """Returns how many alerts moved, so the result shows up in Celery's log."""
    return escalate_due_alerts()
