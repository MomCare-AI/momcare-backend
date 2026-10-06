"""The scheduled job for weekly care plans -- one function, two ways to run it.

``manage.py sweep_care_plans`` (cron) and the Celery task ``care_plans.sweep``
(django-celery-beat) both call ``run_sweep``, so there is one behaviour to reason about.

It does two things:

1. **Week start.** At 12 am where she lives, a new pregnancy week begins; this writes that
   week's plan for every active pregnancy that has readings but no plan for the week she is
   now in -- including a patient who sent no readings (her plan is built from the whole
   month's readings). A reading normally gets there first; this covers a quiet week, and it
   is what makes the plan ready at the start of the week rather than at her first reading.
2. **Retry what failed.** A week whose creation failed outright (nothing was written) is
   simply created again by step 1 on the next run, and a week still on the generic
   fallback (the AI service was down or answered badly) gets its real, personalised plan,
   redoing only the sections that are stuck.

Safe to run as often as you like: a week that already has a plan is left alone, and so is a
plan that is not on the fallback. Run it every 15 minutes or so -- frequent enough that a
failure is retried within minutes, and every time zone's midnight is caught.
"""

import logging

from django.db.models import OuterRef, Q, Subquery

from momcare_platform.core.common.rls import bypass_rls
from momcare_platform.core.patients.models import Pregnancy
from momcare_platform.modules.pregnancy.care_plans.models import CarePlan, CarePlanSectionVersion, CareWeek
from momcare_platform.modules.pregnancy.care_plans.services import (
    local_today,
    replan_current_week,
    start_week_if_needed,
)
from momcare_platform.modules.pregnancy.vitals.models import RiskAssessment

# A long outage must not turn one sweep into hundreds of slow retries; whatever is
# left is picked up by the next run.
MAX_FALLBACK_RETRIES_PER_RUN = 50

logger = logging.getLogger(__name__)


def _start_weeks() -> int:
    # Sweeps every hospital in one pass, by design -- the same sanctioned bypass
    # escalate_alerts uses. Scoped per pregnancy below, so one transaction never
    # spans every patient's model calls.
    with bypass_rls():
        has_readings = RiskAssessment.objects.filter(pregnancy=OuterRef("pk"), reading__isnull=False)
        ids = list(
            Pregnancy.objects.filter(status=Pregnancy.STATUS_ACTIVE, patient__is_active=True)
            .filter(pk__in=has_readings.values("pregnancy"))
            .values_list("pk", flat=True)
        )
    started = 0
    for pregnancy_id in ids:
        # One patient's failure must never stop everyone else's week from starting; the
        # failed one is simply created again on the next run.
        try:
            with bypass_rls():
                pregnancy = Pregnancy.objects.select_related("patient__organization", "patient__location").get(
                    pk=pregnancy_id
                )
                if start_week_if_needed(pregnancy):
                    started += 1
        except Exception:
            logger.exception("care plan sweep: starting the week failed for pregnancy %s", pregnancy_id)
    return started


def _retry_fallback_weeks() -> int:
    def newest_is_fallback(section):
        return Subquery(
            CarePlanSectionVersion.objects.filter(care_plan=OuterRef("care_plan"), section=section)
            .order_by("-created_at")
            .values("is_fallback")[:1]
        )

    with bypass_rls():
        # "Today" differs by time zone; a week still on the fallback is retried whichever
        # day it covers per location, so list candidates by their own dates.
        candidates = list(
            CareWeek.objects.filter(
                care_plan__pregnancy__status=Pregnancy.STATUS_ACTIVE,
                care_plan__pregnancy__patient__is_active=True,
            )
            .annotate(
                nutrition_fallback=newest_is_fallback("nutrition"),
                exercise_fallback=newest_is_fallback("exercise"),
            )
            .filter(Q(nutrition_fallback=True) | Q(exercise_fallback=True))
            .select_related("care_plan__pregnancy__patient__location")
            .order_by("-week_start")[: MAX_FALLBACK_RETRIES_PER_RUN * 4]
        )
    retried = 0
    for week in candidates:
        if retried >= MAX_FALLBACK_RETRIES_PER_RUN:
            break
        pregnancy = week.care_plan.pregnancy
        if not (week.week_start <= local_today(pregnancy) <= week.week_end):
            continue  # only the week she is in is worth retrying
        try:
            with bypass_rls():
                replan_current_week(CarePlan.objects.get(pk=week.care_plan_id), reason="", retry=True)
            retried += 1
        except Exception:
            logger.exception("care plan sweep: retrying the fallback failed for plan %s", week.care_plan_id)
    return retried


def run_sweep() -> tuple[int, int]:
    """-> (weeks started, weeks retried)."""
    return _start_weeks(), _retry_fallback_weeks()
