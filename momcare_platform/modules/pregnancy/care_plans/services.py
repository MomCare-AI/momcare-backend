"""Monthly Care Plan -- weeks, generation, reading advice, staff edits, corrections, status.

Read docs/design/2026-10-06-weekly-care-plan-design.md (and 2026-10-05 for the monthly
container). The shape in one breath: nutrition and exercise are written once per
**pregnancy week** (``CareWeek``), from a progress summary computed by code. Each
later reading is compared with the state that week's plan was written for
(``state.assess_change``): a structural change re-plans at once; a worse state or a
medium/high risk earns short ``ReadingAdvice`` and only a worse state that
*persists* re-plans the week. Doctor edits are a separate layer applied at read time
(``layered_section``), so regenerating never destroys one.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, time, timedelta
from typing import NamedTuple

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from momcare_platform.core.ai import openrouter_client
from momcare_platform.core.ai.services import get_ai_config
from momcare_platform.core.common.obstetrics import (
    calculate_gestational_age,
    care_plan_month,
    care_plan_month_bounds,
    pregnancy_week,
    pregnancy_week_bounds,
    trimester_for,
)
from momcare_platform.core.common.rls import bypass_rls
from momcare_platform.core.patients.models import Patient, Pregnancy

from .guardrails import (
    BASIS_AI,
    InvalidPlan,
    banned_in_content,
    build_sources,
    excluded_foods,
    fallback_advice,
    fallback_content,
    finalize_advice,
    finalize_section,
    find_banned,
    normalize_item_key,
    remove_allergens_from_nutrition,
)
from .models import (
    SECTION_EXERCISE,
    SECTION_NUTRITION,
    CarePlan,
    CarePlanAdjustment,
    CarePlanMedication,
    CarePlanNote,
    CarePlanSectionVersion,
    CareWeek,
    HospitalPreference,
    PlanCorrection,
    ReadingAdvice,
)
from .progress import ReadingRow, build_progress
from .prompts import build_advice_prompt, build_prompt
from .state import (
    CHANGE_STRUCTURAL,
    CHANGE_WORSE,
    CareState,
    abnormal_axes,
    age_band,
    assess_change,
    carry_forward_unknown_vitals,
    vitals_from_reading,
)

logger = logging.getLogger(__name__)

SECTIONS = (SECTION_NUTRITION, SECTION_EXERCISE)
LIST_NAMES = {
    SECTION_NUTRITION: ("meals", "foods_to_eat", "foods_to_avoid"),
    SECTION_EXERCISE: ("activities", "avoid"),
}
# Staff-facing item fields a doctor may write on an add/edit.
ITEM_FIELDS = ("text", "slot", "duration_minutes", "frequency_per_week", "intensity")

MAX_PREFERENCES_IN_PROMPT = 10
# Distinct staff members who must correct the same item before the hospital
# admin is asked to turn it into a preference.
SUGGESTION_THRESHOLD = 3
GENERATION_MAX_TOKENS = 1500
GENERATION_TIMEOUT_SECONDS = 60.0  # includes the live web search (about 6 s in practice)
ADVICE_MAX_TOKENS = 500
ADVICE_TIMEOUT_SECONDS = 15.0

# A worse-than-plan state that is still there on the next reading re-plans the week:
# this many CONSECUTIVE worse readings, counted by readings alone (there is no time
# rule). One worse reading earns quick advice; the third in a row changes the plan; a
# reading that is no longer worse starts the count again. A product decision awaiting
# clinical review.
PERSISTENCE_MIN_READINGS = 3


class PlanLocked(Exception):
    """The plan is finalized: reopen it before editing."""


class InvalidTransition(Exception):
    """A status change that isn't allowed from the plan's current status."""


class InvalidEdit(Exception):
    """A staff edit that cannot be applied; ``str(exc)`` is safe to show."""


# ── plan lookup / creation ────────────────────────────────────────────────


def ensure_care_plan(pregnancy: Pregnancy, *, on_date=None) -> CarePlan | None:
    """This pregnancy's plan for the 30-day block containing ``on_date``,
    created if needed. None when there is no EDD -- an unknown gestational age
    must stay visibly unknown rather than become "month 1"."""
    on_date = on_date or timezone.localdate()
    month = care_plan_month(calculate_gestational_age(pregnancy.edd, on_date))
    if month is None or pregnancy.edd is None:
        return None
    start, end = care_plan_month_bounds(pregnancy.edd, month)
    plan, _ = CarePlan.objects.get_or_create(
        pregnancy=pregnancy,
        month_number=month,
        defaults={"period_start": start, "period_end": end},
    )
    return plan


def latest_version(plan: CarePlan, section: str) -> CarePlanSectionVersion | None:
    return plan.versions.filter(section=section).order_by("-created_at").first()


def local_today(pregnancy: Pregnancy):
    """Today's date where she lives. A pregnancy week starts at 12 am in the hospital
    location's own time zone, not the server's."""
    return timezone.localdate(timezone=pregnancy.patient.location.timezone)


def ensure_week(pregnancy: Pregnancy, *, on_date=None) -> tuple[CarePlan, CareWeek, bool] | None:
    """The CareWeek for the pregnancy week containing ``on_date`` (today), created if
    needed, inside the monthly plan that contains the week's FIRST day -- so a week
    that straddles a month edge is never split. Returns ``(plan, week, created)``, or
    None when there is no EDD (an unknown gestational age stays unknown)."""
    on_date = on_date or local_today(pregnancy)
    number = pregnancy_week(calculate_gestational_age(pregnancy.edd, on_date))
    if number is None or pregnancy.edd is None:
        return None
    start, end = pregnancy_week_bounds(pregnancy.edd, number)
    plan = ensure_care_plan(pregnancy, on_date=start)
    if plan is None:
        return None
    week, created = CareWeek.objects.get_or_create(
        care_plan=plan, week_number=number, defaults={"week_start": start, "week_end": end}
    )
    return plan, week, created


def current_week(pregnancy: Pregnancy) -> CareWeek | None:
    today = local_today(pregnancy)
    return (
        CareWeek.objects.filter(care_plan__pregnancy=pregnancy, week_start__lte=today, week_end__gte=today)
        .select_related("care_plan")
        .first()
    )


# ── building the state ────────────────────────────────────────────────────


def _previous_plan(plan: CarePlan) -> CarePlan | None:
    return (
        CarePlan.objects.filter(pregnancy=plan.pregnancy, month_number__lt=plan.month_number)
        .exclude(current_state_key="")
        .order_by("-month_number")
        .first()
    )


def _patient_age(patient: Patient, reading) -> int | None:
    if patient.date_of_birth:
        today = timezone.localdate()
        dob = patient.date_of_birth
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    return getattr(reading, "age", None)


def build_state(plan: CarePlan, pregnancy: Pregnancy, assessment, reading) -> CareState:
    patient = pregnancy.patient
    previous_vitals = (plan.current_state or {}).get("vitals")
    if previous_vitals is None:
        previous = _previous_plan(plan)
        previous_vitals = (previous.current_state or {}).get("vitals") if previous else None
    return CareState(
        trimester=trimester_for(calculate_gestational_age(pregnancy.edd)),
        risk=assessment.final_risk_level,
        vitals=carry_forward_unknown_vitals(vitals_from_reading(reading), previous_vitals),
        region=patient.organization.region or "",
        age_band=age_band(_patient_age(patient, reading)),
        allergies=tuple(sorted({a.strip().lower() for a in (patient.food_allergies or []) if str(a).strip()})),
        dietary_preference=patient.dietary_preference or "",
        factors=tuple(f for f in Pregnancy.FACTOR_FIELDS if getattr(pregnancy, f) == Pregnancy.YES),
    )


def _one_line(text: str) -> str:
    return " ".join(str(text).split())[:300]


def approved_preferences(organization, region: str, trimester, section: str) -> list[str]:
    """This hospital's approved guidance for one section (food advice is never
    mixed into the exercise prompt, or the other way round)."""
    prefs = HospitalPreference.objects.filter(
        organization=organization,
        status=HospitalPreference.STATUS_APPROVED,
        region=region,
        section=section,
    ).filter(Q(trimester=trimester) | Q(trimester__isnull=True))
    ordered = prefs.order_by("-supporting_staff")[:MAX_PREFERENCES_IN_PROMPT]
    return [_one_line(p.payload.get("guidance", "")) for p in ordered]


def adjustment_lines(plan: CarePlan, section: str) -> list[str]:
    lines = []
    for adj in plan.adjustments.filter(is_active=True, section=section):
        text = adj.content.get("text", "")
        if adj.action == CarePlanAdjustment.ACTION_REMOVE:
            lines.append(f"removed {adj.item_key} from {adj.list_name}")
        elif adj.action == CarePlanAdjustment.ACTION_EDIT:
            lines.append(_one_line(f"replaced {adj.item_key} in {adj.list_name} with: {text}"))
        else:
            lines.append(_one_line(f"added to {adj.list_name}: {text}"))
    return lines


# ── generating ────────────────────────────────────────────────────────────


def safe_fallback(section: str, risk: str, allergens) -> dict:
    """The generic safe plan for one section, used when the model fails or stays
    invalid for *that section*. The other section is unaffected."""
    content = deepcopy(fallback_content(risk)[section])
    if section == SECTION_NUTRITION:
        content, _removed = remove_allergens_from_nutrition(content, allergens)
        if not content["meals"]:
            content["meals"] = [
                {"slot": "lunch", "item_key": "vegetables", "text": "Cooked vegetables with plain rice or roti"}
            ]
    return content


class SectionRequest(NamedTuple):
    """Everything one section's model request needs, gathered from the database up
    front so that sending it touches no database (it runs in a worker thread)."""

    section: str
    prompt: str
    model: str
    risk: str
    excluded: list


def prepare_section_request(
    section: str,
    state: CareState,
    *,
    preferences: list[str],
    adjustments: list[str],
    country: str = "",
    progress: str = "",
) -> SectionRequest:
    config = get_ai_config()
    prompt = build_prompt(
        section, state, preferences=preferences, adjustments=adjustments, country=country, progress=progress
    )
    return SectionRequest(
        section, prompt, config.current_model, state.risk, excluded_foods(state.allergies, state.dietary_preference)
    )


def run_section_request(request: SectionRequest):
    """-> (content, removed_for_allergy, model_name, is_fallback) for ONE section.
    Nutrition and exercise are separate requests, answers, validations and
    fallbacks. Never raises: a patient must always have a plan, so any failure
    ends in that section's safe baseline. Touches no database, so the two sections
    can be sent at the same time."""
    web_search = True
    for attempt in (1, 2, 3):
        # The last try is without the search: a model that keeps answering in prose or
        # unusable JSON once it has read web pages can still write the plan from its
        # own knowledge -- labelled "generated by AI", better than the generic baseline.
        web_search = web_search and attempt < 3
        answer = openrouter_client.generate_researched(
            request.prompt,
            model=request.model,
            max_tokens=GENERATION_MAX_TOKENS,
            timeout=GENERATION_TIMEOUT_SECONDS,
            web_search=web_search,
        )
        if answer is None:
            if web_search:
                # The search itself may be what failed: try again without it. The plan
                # is then the model's own work and is labelled "generated by AI".
                web_search = False
                continue
            break  # a dead service: retrying only slows the reading down
        try:
            content, removed = finalize_section(
                answer.text,
                request.section,
                risk=request.risk,
                allergens=request.excluded,
                citations=answer.citations,
            )
            return content, removed, request.model, False
        except InvalidPlan as exc:
            logger.warning("care plan %s output rejected (attempt %s): %s", request.section, attempt, exc)
    return safe_fallback(request.section, request.risk, request.excluded), [], request.model, True


def run_section_requests(requests: dict[str, SectionRequest]) -> dict:
    """Send every section's request at the same time (about half the waiting of one
    after the other). An exception inside a request still reaches the caller."""
    if len(requests) < 2:
        return {section: run_section_request(req) for section, req in requests.items()}
    with ThreadPoolExecutor(max_workers=len(requests), thread_name_prefix="care-plan") as pool:
        futures = {section: pool.submit(run_section_request, req) for section, req in requests.items()}
        return {section: future.result() for section, future in futures.items()}


def generate_section_content(section: str, state: CareState, **kwargs):
    """One section, start to finish (prepare, send, validate)."""
    return run_section_request(prepare_section_request(section, state, **kwargs))


def _reopen_for_new_content(plan: CarePlan) -> None:
    """New generated content is something nobody has reviewed. A reviewed or
    finalized plan goes back to in_progress -- the patient's safety needs the
    update more than the sign-off needs to stay intact."""
    if plan.status != CarePlan.STATUS_IN_PROGRESS:
        plan.status = CarePlan.STATUS_IN_PROGRESS
        plan.reviewed_by = plan.reviewed_at = plan.finalized_by = plan.finalized_at = None


def _day_start(day, tz) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz)


def _rows(pregnancy: Pregnancy, start_day, end_day) -> list[ReadingRow]:
    """Her readings from the start of ``start_day`` up to (not including) the start
    of ``end_day``, each with the risk it was scored at -- one row per reading (a
    manual re-score adds a second assessment for the same reading; the newest wins)."""
    from momcare_platform.modules.pregnancy.vitals.models import RiskAssessment  # noqa: PLC0415

    tz = pregnancy.patient.location.timezone
    assessments = (
        RiskAssessment.objects.filter(
            pregnancy=pregnancy,
            reading__isnull=False,
            reading__recorded_at__gte=_day_start(start_day, tz),
            reading__recorded_at__lt=_day_start(end_day, tz),
        )
        .select_related("reading")
        .order_by("assessed_at")
    )
    by_reading = {a.reading_id: a for a in assessments if a.reading is not None}
    rows = []
    for a in sorted(by_reading.values(), key=lambda a: a.reading.recorded_at):  # type: ignore[union-attr]
        r = a.reading
        assert r is not None
        rows.append(
            ReadingRow(
                recorded_at=r.recorded_at,
                risk=a.final_risk_level,
                vitals=vitals_from_reading(r),
                values={
                    "systolic_bp": r.systolic_bp,
                    "diastolic_bp": r.diastolic_bp,
                    "heart_rate": r.heart_rate,
                    "hemoglobin": r.hemoglobin,
                    "blood_glucose": r.blood_glucose,
                },
            )
        )
    return rows


def latest_risk_level(pregnancy: Pregnancy) -> str:
    """The risk level of her most recent reading ("" if she has none). Read straight from
    the readings, not from the plan: a high-risk patient must keep seeing "contact your
    care team" for as long as her latest reading is high, even though the week's plan was
    written at high risk and so earns no new quick advice for the same state."""
    from momcare_platform.modules.pregnancy.vitals.models import RiskAssessment  # noqa: PLC0415

    return (
        RiskAssessment.objects.filter(pregnancy=pregnancy, reading__isnull=False)
        .order_by("-reading__recorded_at", "-assessed_at")
        .values_list("final_risk_level", flat=True)
        .first()
        or ""
    )


def build_week_progress(plan: CarePlan, pregnancy: Pregnancy, week: CareWeek, state: CareState, reading) -> dict:
    """Last week's conditions and this month's trend, computed from her readings."""
    last_week = []
    assert pregnancy.edd is not None  # a week only exists for a pregnancy with a due date
    if week.week_number > 0:
        previous_start, _ = pregnancy_week_bounds(pregnancy.edd, week.week_number - 1)
        last_week = _rows(pregnancy, previous_start, week.week_start)
    month_before_week = _rows(pregnancy, plan.period_start, week.week_start)
    prior_week = []
    if week.week_number > 1:
        before_start, _ = pregnancy_week_bounds(pregnancy.edd, week.week_number - 2)
        prior_week = _rows(pregnancy, before_start, previous_start)
    return build_progress(
        week_number=week.week_number,
        last_week=last_week,
        month_before_week=month_before_week,
        focus_axes=abnormal_axes(state),
        last_reading_at=reading.recorded_at if reading else None,
        prior_week=prior_week,
        week_start=week.week_start,
    )


def generate_week_plan(
    plan: CarePlan,
    week: CareWeek,
    pregnancy: Pregnancy,
    state: CareState,
    reading,
    *,
    reason: str = "",
    sections=SECTIONS,
    retry: bool = False,
) -> int:
    """Write the week's nutrition and exercise (two separate requests). A first plan
    for the week builds the progress summary; a re-plan keeps it and records why.
    ``retry`` redoes only sections that are on the generic fallback and writes nothing
    if they fail again. Returns how many versions were written."""
    if not week.progress:
        week.progress = build_week_progress(plan, pregnancy, week, state, reading)
    stored = week.progress or {}

    requests = {
        section: prepare_section_request(
            section,
            state,
            preferences=approved_preferences(pregnancy.patient.organization, state.region, state.trimester, section),
            adjustments=adjustment_lines(plan, section),
            # The hospital's country -- where the patient lives, not who she is, so it is
            # safe to send; region alone made the model suggest foods from the wrong half of Asia.
            country=pregnancy.patient.organization.country or "",
            # Each section is told how she is doing in ITS OWN vitals (an older week that
            # was stored before this existed falls back to the overall summary).
            progress=(stored.get(section) or {}).get("text") or stored.get("text", ""),
        )
        for section in sections
    }
    results = run_section_requests(requests)
    if retry:
        # A retry that failed again writes nothing: no pile of identical generic versions.
        results = {sec: res for sec, res in results.items() if not res[3]}
        if not results:
            return 0

    for section, (content, removed, model_name, is_fallback) in results.items():
        _write_version(plan, week, section, state, reading, content, removed, model_name, is_fallback)

    if not retry:
        if week.baseline_state_key:  # this week already had a plan: it is a re-plan
            week.replans += 1
        week.last_replan_reason = reason
        week.baseline_state, week.baseline_state_key = state.as_dict(), state.key
        week.worse_since, week.worse_readings = None, 0
        plan.current_state, plan.current_state_key = state.as_dict(), state.key
    plan.last_outcome = CarePlan.OUTCOME_NEW
    plan.last_evaluated_at = timezone.now()
    _reopen_for_new_content(plan)
    week.save()
    plan.save()
    return len(results)


def _write_version(plan, week, section, state, reading, content, removed, model_name, is_fallback):
    inputs = state.as_dict()
    inputs["removed_for_allergy"] = removed
    inputs["as_of_reading"] = reading.recorded_at.isoformat() if reading else None
    CarePlanSectionVersion.objects.create(
        care_plan=plan,
        week=week,
        section=section,
        content=content,
        inputs=inputs,
        state_key=state.key,
        source_reading=reading,
        model_name=model_name,
        is_fallback=is_fallback,
    )


def _plan_lines(plan: CarePlan) -> list[str]:
    """What the week's plan says, as short lines for the advice prompt."""
    lines = []
    nutrition = layered_section(plan, SECTION_NUTRITION)["content"] or {}
    exercise = layered_section(plan, SECTION_EXERCISE)["content"] or {}
    if nutrition.get("foods_to_avoid"):
        lines.append("Foods to avoid this week: " + "; ".join(i["text"] for i in nutrition["foods_to_avoid"]))
    if exercise.get("activities"):
        lines.append("Activities this week: " + "; ".join(a["text"] for a in exercise["activities"]))
    if exercise.get("avoid"):
        lines.append("Activities to avoid: " + "; ".join(a["text"] for a in exercise["avoid"]))
    return [_one_line(line) for line in lines]


def _plan_sources(plan: CarePlan) -> dict:
    """The sources of the weekly plan she is following (nutrition's, else exercise's).
    A quick tip is drawn from that researched plan, so it carries the same sources and
    the same "generated by AI" label if the plan had no official document."""
    for section in (SECTION_NUTRITION, SECTION_EXERCISE):
        version = latest_version(plan, section)
        if version is not None and not version.is_fallback:
            content = version.content
            return {
                "basis": content.get("basis", BASIS_AI),
                "sources": content.get("sources", []),
                "source_links": content.get("source_links", []),
            }
    return build_sources([])


def generate_reading_advice(plan: CarePlan, week: CareWeek, pregnancy: Pregnancy, state: CareState, reading):
    """Short advice for this reading, looking at the weekly plan she is following.
    Never raises: a patient who is medium/high risk must always get something."""
    config = get_ai_config()
    prompt = build_advice_prompt(
        state, plan_lines=_plan_lines(plan), country=pregnancy.patient.organization.country or ""
    )
    content, is_fallback = None, False
    plan_sources = _plan_sources(plan)
    for attempt in (1, 2):
        raw = openrouter_client.generate(
            prompt, model=config.current_model, max_tokens=ADVICE_MAX_TOKENS, timeout=ADVICE_TIMEOUT_SECONDS
        )
        if raw is None:
            break
        try:
            content = finalize_advice(
                raw,
                risk=state.risk,
                allergens=excluded_foods(state.allergies, state.dietary_preference),
                sources=plan_sources,
            )
            break
        except InvalidPlan as exc:
            logger.warning("reading advice rejected (attempt %s): %s", attempt, exc)
    if content is None:
        content, is_fallback = fallback_advice(state.risk), True
    return ReadingAdvice.objects.create(
        care_plan=plan,
        week=week,
        reading=reading,
        risk_level=state.risk,
        state_key=state.key,
        content=content,
        model_name=config.current_model,
        is_fallback=is_fallback,
    )


def process_reading(plan: CarePlan, week: CareWeek, pregnancy: Pregnancy, state: CareState, reading, *, created: bool):
    """What one reading does to its week. ``created``: the week had no plan yet, so
    this reading writes it (from last week's readings and the month)."""
    if created:
        generate_week_plan(plan, week, pregnancy, state, reading, reason="week start")
        return "week_plan"

    change = assess_change(CareState.from_dict(week.baseline_state) if week.baseline_state_key else None, state)

    if change == CHANGE_STRUCTURAL:
        generate_week_plan(
            plan,
            week,
            pregnancy,
            state,
            reading,
            reason="your stage of pregnancy, allergies, diet or conditions changed",
        )
        return "replan_structural"

    if change == CHANGE_WORSE:
        if week.worse_since is None:
            week.worse_since, week.worse_readings = reading.recorded_at, 1
        else:
            week.worse_readings += 1
        if week.worse_readings >= PERSISTENCE_MIN_READINGS:
            generate_week_plan(
                plan,
                week,
                pregnancy,
                state,
                reading,
                reason="your readings stayed worse than this week's plan expected",
            )
            return "replan_persistent"
    else:
        week.worse_since, week.worse_readings = None, 0

    outcome = "nothing"
    if state.risk in ("medium", "high") and state.key not in (week.baseline_state_key, week.last_advice_state_key):
        generate_reading_advice(plan, week, pregnancy, state, reading)
        week.last_advice_state_key = state.key
        outcome = "advice"
    week.save()
    plan.last_outcome = CarePlan.OUTCOME_CONTINUED
    plan.last_evaluated_at = timezone.now()
    plan.save(update_fields=["last_outcome", "last_evaluated_at", "updated_at"])
    return outcome


def _run(assessment) -> None:
    reading = assessment.reading
    if reading is None:
        return
    pregnancy = Pregnancy.objects.select_related("patient__organization", "patient__location").get(
        pk=assessment.pregnancy_id
    )
    if pregnancy.status != Pregnancy.STATUS_ACTIVE or not pregnancy.patient.is_active:
        return
    found = ensure_week(pregnancy)
    if found is None:
        return
    plan, week, created = found
    plan = CarePlan.objects.select_for_update().get(pk=plan.pk)  # one reading at a time per plan
    week = CareWeek.objects.get(pk=week.pk)
    process_reading(plan, week, pregnancy, build_state(plan, pregnancy, assessment, reading), reading, created=created)


def generate_for_assessment(assessment) -> None:
    """The post-save hook body. Never raises: a failing care plan must not fail
    (or roll back) the reading that triggered it. The savepoint keeps a database
    error here from poisoning the surrounding request transaction."""
    try:
        with transaction.atomic():
            _run(assessment)
    except Exception:
        logger.exception("care plan generation failed for assessment %s", getattr(assessment, "pk", None))


def process_assessment_by_id(assessment_id) -> None:
    """The background-worker entry point: load the assessment and do what the hook does.
    A worker has no request, so no hospital is set -- like the sweep it runs as the
    sanctioned cross-tenant path, and only ever touches this one reading's pregnancy."""
    from momcare_platform.modules.pregnancy.vitals.models import RiskAssessment  # noqa: PLC0415

    with bypass_rls():
        assessment = RiskAssessment.objects.select_related("reading").filter(pk=assessment_id).first()
        if assessment is not None:
            generate_for_assessment(assessment)


def schedule_for_assessment(assessment) -> None:
    """The post-save hook. With ``CARE_PLAN_GENERATE_IN_BACKGROUND`` the reading is saved
    first and a Celery worker writes the plan after the commit (so it never waits on the
    model); otherwise the plan is written right here. Never raises."""
    if not settings.CARE_PLAN_GENERATE_IN_BACKGROUND:
        generate_for_assessment(assessment)
        return
    assessment_id = str(assessment.pk)

    def enqueue() -> None:
        from momcare_platform.modules.pregnancy.care_plans.tasks import process_assessment_task  # noqa: PLC0415

        try:
            process_assessment_task.delay(assessment_id)
        except Exception:
            # No broker: better a slow reading than a patient with no plan.
            logger.exception("could not queue the care plan for assessment %s; writing it now", assessment_id)
            try:
                process_assessment_by_id(assessment_id)
            except Exception:
                logger.exception("care plan generation failed for assessment %s", assessment_id)

    transaction.on_commit(enqueue)


def _latest_assessment(pregnancy: Pregnancy):
    from momcare_platform.modules.pregnancy.vitals.models import RiskAssessment  # noqa: PLC0415

    return (
        RiskAssessment.objects.filter(pregnancy=pregnancy, reading__isnull=False)
        .select_related("reading")
        .order_by("-assessed_at")
        .first()
    )


# How long after a reading its plan update is still "on its way". Past this, a plan that
# is not there (the worker was down) is no longer described as "being updated".
UPDATE_PENDING_WINDOW = timedelta(minutes=10)


def plan_progress_flags(pregnancy: Pregnancy, plan: CarePlan | None, has_week_today: bool) -> dict:
    """What the screen should say while the background worker is writing a plan:
    ``preparing`` (she has readings but no plan for this week yet: show "your plan is being
    prepared") and ``update_pending`` (the plan shown has not yet looked at her newest
    reading). Both clear by themselves once the plan is written."""
    latest = _latest_assessment(pregnancy)
    if latest is None:  # no readings: nothing is on its way (she is in the "missing" workflow)
        return {"preparing": False, "update_pending": False, "status_message": None}
    preparing = not has_week_today
    pending = bool(
        has_week_today
        and plan is not None
        and plan.last_evaluated_at is not None
        and latest.assessed_at > plan.last_evaluated_at
        and timezone.now() - latest.assessed_at < UPDATE_PENDING_WINDOW
    )
    message = (
        "Your plan is being prepared. It will appear here in a few moments."
        if preparing
        else "Your plan is being updated for your latest reading."
        if pending
        else None
    )
    return {"preparing": preparing, "update_pending": pending, "status_message": message}


def start_week_if_needed(pregnancy: Pregnancy) -> bool:
    """The week-start trigger for a pregnancy nobody has read this week (the sweep
    calls this): writes the week's plan from the month's readings, or her last
    readings. A pregnancy with no readings at all gets nothing. True if a plan was written."""
    assessment = _latest_assessment(pregnancy)
    if assessment is None:
        return False
    with transaction.atomic():
        found = ensure_week(pregnancy)
        if found is None:
            return False
        plan, week, created = found
        if not created:
            return False
        plan = CarePlan.objects.select_for_update().get(pk=plan.pk)
        week = CareWeek.objects.get(pk=week.pk)
        generate_week_plan(
            plan,
            week,
            pregnancy,
            build_state(plan, pregnancy, assessment, assessment.reading),
            assessment.reading,
            reason="week start",
        )
    return True


def replan_current_week(plan: CarePlan, *, reason: str, retry: bool = False) -> None:
    """Re-plan this plan's current week from her newest reading -- after allergies or
    conditions change (so a new allergy cannot wait), or (``retry``) to give a week
    stuck on the generic fallback its real plan."""
    pregnancy = Pregnancy.objects.select_related("patient__organization", "patient__location").get(
        pk=plan.pregnancy_id
    )
    today = local_today(pregnancy)
    week = plan.weeks.filter(week_start__lte=today, week_end__gte=today).first()
    assessment = _latest_assessment(pregnancy)
    if week is None or assessment is None:
        return
    with transaction.atomic():
        locked = CarePlan.objects.select_for_update().get(pk=plan.pk)
        week = CareWeek.objects.get(pk=week.pk)
        state = build_state(locked, pregnancy, assessment, assessment.reading)
        if retry:
            stuck = [s for s in SECTIONS if (v := latest_version(locked, s)) is not None and v.is_fallback]
            generate_week_plan(locked, week, pregnancy, state, assessment.reading, sections=stuck, retry=True)
        else:
            generate_week_plan(locked, week, pregnancy, state, assessment.reading, reason=reason)


# ── what staff and patients see ───────────────────────────────────────────


def _rotation_filter(rotation, removed_keys):
    if not removed_keys:
        return rotation
    needles = [k.replace("_", " ") for k in removed_keys]
    out = []
    for day in rotation:
        meals = [m for m in day.get("meals", []) if not any(n in m.lower() for n in needles)]
        out.append({**day, "meals": meals})
    return out


def layered_section(plan: CarePlan, section: str) -> dict:
    """The newest generated version with staff adjustments applied on top, every
    item tagged ``source`` (ai/staff). Read-time only: the stored version is
    never mutated, which is what lets a regeneration keep the doctor's edits."""
    version = latest_version(plan, section)
    if version is None:
        return {
            "content": None,
            "sources": [],
            "source_links": [],
            "basis": None,
            "generated_at": None,
            "is_fallback": False,
            "based_on_reading_at": None,
        }
    content = deepcopy(version.content)
    sources = content.pop("sources", [])
    source_links = content.pop("source_links", [])
    basis = content.pop("basis", BASIS_AI)
    for name in LIST_NAMES[section]:
        for item in content.get(name, []):
            item["source"] = "ai"
    removed_keys = []
    for adj in plan.adjustments.filter(section=section, is_active=True).order_by("created_at"):
        items = content.setdefault(adj.list_name, [])
        if adj.action == CarePlanAdjustment.ACTION_REMOVE:
            items[:] = [i for i in items if i["item_key"] != adj.item_key]
            removed_keys.append(adj.item_key)
        elif adj.action == CarePlanAdjustment.ACTION_EDIT:
            for i, item in enumerate(items):
                if item["item_key"] == adj.item_key:
                    items[i] = {
                        **item,
                        **adj.content,
                        "item_key": adj.item_key,
                        "source": "staff",
                        "adjustment_id": str(adj.id),
                    }
                    break
            else:
                items.append(
                    {**adj.content, "item_key": adj.item_key, "source": "staff", "adjustment_id": str(adj.id)}
                )
        else:
            items.append({**adj.content, "item_key": adj.item_key, "source": "staff", "adjustment_id": str(adj.id)})
    if section == SECTION_NUTRITION:
        content["weekly_rotation"] = _rotation_filter(content.get("weekly_rotation", []), removed_keys)
    return {
        "content": content,
        "sources": sources,
        "source_links": source_links,
        "basis": basis,
        "generated_at": version.created_at,
        "is_fallback": version.is_fallback,
        "based_on_reading_at": (version.inputs or {}).get("as_of_reading"),
    }


# ── staff edits ───────────────────────────────────────────────────────────


def touch_for_edit(plan: CarePlan) -> None:
    """Any staff edit: refuse on a finalized plan, and return a reviewed one to
    in_progress (what was reviewed has changed)."""
    if plan.status == CarePlan.STATUS_FINALIZED:
        raise PlanLocked("This care plan is finalized. Reopen it before editing.")
    if plan.status == CarePlan.STATUS_REVIEWED:
        plan.status = CarePlan.STATUS_IN_PROGRESS
        plan.reviewed_by = plan.reviewed_at = None
        plan.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])


def _check_text_is_safe(content: dict) -> None:
    banned = banned_in_content(content)
    if banned:
        raise InvalidEdit(
            "Medicines, supplements, doses and calorie or weight targets are not allowed in the nutrition or "
            f"exercise plan: {sorted({b.lower() for b in banned})}."
        )


def _find_item(layered: dict, list_name: str, item_key: str) -> dict | None:
    for item in (layered["content"] or {}).get(list_name, []):
        if item["item_key"] == item_key:
            return item
    return None


def _clean_item_content(content) -> dict:
    if not isinstance(content, dict):
        raise InvalidEdit("content must be an object.")
    cleaned = {k: content[k] for k in ITEM_FIELDS if k in content}
    if not str(cleaned.get("text", "")).strip():
        raise InvalidEdit("content.text is required.")
    cleaned["text"] = str(cleaned["text"]).strip()
    return cleaned


def create_adjustment(plan, user, *, section, list_name, action, item_key, content=None) -> CarePlanAdjustment:
    if section not in SECTIONS or list_name not in LIST_NAMES[section]:
        raise InvalidEdit("Unknown section or list.")
    if action not in (CarePlanAdjustment.ACTION_ADD, CarePlanAdjustment.ACTION_EDIT, CarePlanAdjustment.ACTION_REMOVE):
        raise InvalidEdit("action must be add, edit or remove.")
    touch_for_edit(plan)

    layered = layered_section(plan, section)
    if layered["content"] is None:
        raise InvalidEdit("This plan has no generated content yet.")

    if action == CarePlanAdjustment.ACTION_ADD:
        cleaned = _clean_item_content(content)
        item_key = normalize_item_key(item_key or cleaned["text"])
        if _find_item(layered, list_name, item_key):
            raise InvalidEdit(f"'{item_key}' is already in the plan. Edit it instead.")
        original = {}
    else:
        item_key = normalize_item_key(item_key)
        existing = _find_item(layered, list_name, item_key)
        if existing is None:
            raise InvalidEdit(f"'{item_key}' is not in the plan.")
        if existing.get("source") == "staff":
            raise InvalidEdit("That item was added by staff; edit or deactivate its own adjustment instead.")
        original = {k: v for k, v in existing.items() if k not in ("source", "adjustment_id")}
        cleaned = _clean_item_content(content) if action == CarePlanAdjustment.ACTION_EDIT else {}
    _check_text_is_safe(cleaned)

    adjustment = CarePlanAdjustment.objects.create(
        care_plan=plan,
        section=section,
        list_name=list_name,
        action=action,
        item_key=item_key,
        content=cleaned,
        added_by=user,
    )
    _log_correction(
        plan, user, section=section, item_key=item_key, action=action, original=original, replacement=cleaned
    )
    return adjustment


def update_adjustment(adjustment: CarePlanAdjustment, user, content) -> CarePlanAdjustment:
    if adjustment.action == CarePlanAdjustment.ACTION_REMOVE:
        raise InvalidEdit("A removal has no content to edit. Deactivate it to bring the item back.")
    touch_for_edit(adjustment.care_plan)
    cleaned = _clean_item_content(content)
    _check_text_is_safe(cleaned)
    adjustment.content = cleaned
    adjustment.save(update_fields=["content", "updated_at"])
    return adjustment


def deactivate(obj, user, reason: str = "") -> None:
    """Soft-delete any Deactivatable care-plan row (adjustment, medication, note)."""
    touch_for_edit(obj.care_plan)
    obj.is_active = False
    obj.deactivated_at = timezone.now()
    obj.deactivated_by = user
    obj.deactivation_reason = reason
    obj.save(update_fields=["is_active", "deactivated_at", "deactivated_by", "deactivation_reason", "updated_at"])


def add_medication(plan, user, text: str) -> CarePlanMedication:
    touch_for_edit(plan)
    return CarePlanMedication.objects.create(care_plan=plan, text=text.strip(), added_by=user)


def add_note(plan, user, text: str) -> CarePlanNote:
    touch_for_edit(plan)
    return CarePlanNote.objects.create(care_plan=plan, text=text.strip(), added_by=user)


def save_text_row(obj, text: str) -> None:
    touch_for_edit(obj.care_plan)
    obj.text = text.strip()
    obj.save(update_fields=["text", "updated_at"])


# ── allergies and conditions (write-through) ──────────────────────────────


@transaction.atomic
def update_allergies_conditions(plan: CarePlan, data: dict) -> None:
    """Writes to the patient and pregnancy records -- the single copy -- then
    re-runs the rule so the plan reflects it straight away."""
    pregnancy = Pregnancy.objects.select_related("patient").get(pk=plan.pregnancy_id)
    patient = pregnancy.patient
    patient_fields, pregnancy_fields = [], []
    if "food_allergies" in data:
        patient.food_allergies = [a.strip() for a in data["food_allergies"] if a.strip()]
        patient_fields.append("food_allergies")
    if "dietary_preference" in data:
        patient.dietary_preference = data["dietary_preference"]
        patient_fields.append("dietary_preference")
    for field in Pregnancy.FACTOR_FIELDS:
        if field in data:
            setattr(pregnancy, field, data[field])
            pregnancy_fields.append(field)
    if patient_fields:
        patient.save(update_fields=[*patient_fields, "updated_at"])
    if pregnancy_fields:
        pregnancy.save(update_fields=[*pregnancy_fields, "updated_at"])
    replan_current_week(plan, reason="your allergies, diet or conditions were updated")


# ── status ────────────────────────────────────────────────────────────────


def review(plan: CarePlan, user) -> None:
    if plan.status != CarePlan.STATUS_IN_PROGRESS:
        raise InvalidTransition("Only an in-progress plan can be marked reviewed.")
    plan.status, plan.reviewed_by, plan.reviewed_at = CarePlan.STATUS_REVIEWED, user, timezone.now()
    plan.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])


def finalize_plan(plan: CarePlan, user) -> None:
    if plan.status != CarePlan.STATUS_REVIEWED:
        raise InvalidTransition("Only a reviewed plan can be finalized.")
    plan.status, plan.finalized_by, plan.finalized_at = CarePlan.STATUS_FINALIZED, user, timezone.now()
    plan.save(update_fields=["status", "finalized_by", "finalized_at", "updated_at"])


def reopen(plan: CarePlan, user) -> None:
    if plan.status == CarePlan.STATUS_IN_PROGRESS:
        raise InvalidTransition("The plan is already in progress.")
    plan.status = CarePlan.STATUS_IN_PROGRESS
    plan.reviewed_by = plan.reviewed_at = plan.finalized_by = plan.finalized_at = None
    plan.save(update_fields=["status", "reviewed_by", "reviewed_at", "finalized_by", "finalized_at", "updated_at"])


# ── corrections -> hospital preferences ───────────────────────────────────


def _log_correction(plan, user, *, section, item_key, action, original, replacement) -> PlanCorrection:
    pregnancy = Pregnancy.objects.select_related("patient__organization").get(pk=plan.pregnancy_id)
    patient = pregnancy.patient
    state = plan.current_state or {}
    correction = PlanCorrection.objects.create(
        organization=patient.organization,
        patient=patient,
        pregnancy=pregnancy,
        care_plan=plan,
        region=patient.organization.region or "",
        trimester=state.get("trimester") or trimester_for(calculate_gestational_age(pregnancy.edd)),
        section=section,
        item_key=item_key,
        action={
            "edit": PlanCorrection.ACTION_REPLACE,
            "remove": PlanCorrection.ACTION_REMOVE,
            "add": PlanCorrection.ACTION_ADD,
        }[action],
        original=original,
        replacement=replacement,
        edited_by=user,
    )
    maybe_suggest_preference(correction)
    return correction


def maybe_suggest_preference(correction: PlanCorrection) -> HospitalPreference | None:
    """Three distinct staff correcting the same item (same hospital, region,
    trimester, section) -> a suggestion for the hospital admin. Counted on the
    item that was *replaced or removed*, not on what replaced it."""
    if correction.action not in (PlanCorrection.ACTION_REPLACE, PlanCorrection.ACTION_REMOVE):
        return None
    key = {
        "organization": correction.organization,
        "region": correction.region,
        "trimester": correction.trimester,
        "section": correction.section,
        "item_key": correction.item_key,
    }
    corrections = PlanCorrection.objects.filter(
        **key, action__in=(PlanCorrection.ACTION_REPLACE, PlanCorrection.ACTION_REMOVE)
    )
    last_rejected = (
        HospitalPreference.objects.filter(**key, status=HospitalPreference.STATUS_REJECTED)
        .order_by("-decided_at")
        .first()
    )
    if last_rejected and last_rejected.decided_at:
        # A rejection is final until three *new* staff members disagree with it.
        corrections = corrections.filter(created_at__gt=last_rejected.decided_at)
    staff_count = corrections.values("edited_by").distinct().count()

    open_pref = HospitalPreference.objects.filter(**key, status__in=HospitalPreference.OPEN_STATUSES).first()
    if open_pref is not None:
        if open_pref.status == HospitalPreference.STATUS_SUGGESTED and staff_count != open_pref.supporting_staff:
            open_pref.supporting_staff = staff_count
            open_pref.save(update_fields=["supporting_staff", "updated_at"])
        return None
    if staff_count < SUGGESTION_THRESHOLD:
        return None

    first = corrections.order_by("created_at").first()
    original_text = ((first.original if first else None) or {}).get("text") or correction.item_key.replace("_", " ")
    replacements = sorted(
        {
            c.replacement["text"]
            for c in corrections.filter(action=PlanCorrection.ACTION_REPLACE)
            if isinstance(c.replacement, dict) and c.replacement.get("text")
        }
    )
    guidance = f'Staff here usually replace or remove "{original_text}".'
    if replacements:
        guidance += " Replacements they used: " + "; ".join(replacements) + "."
    if find_banned(guidance):
        logger.warning("not suggesting a preference for %s: its text contains banned wording", correction.item_key)
        return None
    try:
        with transaction.atomic():
            return HospitalPreference.objects.create(
                **key,
                payload={"item_key": correction.item_key, "guidance": guidance, "replacements": replacements},
                supporting_staff=staff_count,
            )
    except IntegrityError:  # a concurrent correction created it first
        return None


def decide_preference(pref: HospitalPreference, user, *, status: str, guidance: str | None = None) -> None:
    allowed = {
        HospitalPreference.STATUS_APPROVED: (HospitalPreference.STATUS_SUGGESTED,),
        HospitalPreference.STATUS_REJECTED: (HospitalPreference.STATUS_SUGGESTED,),
        HospitalPreference.STATUS_INACTIVE: (HospitalPreference.STATUS_APPROVED,),
    }[status]
    if pref.status not in allowed:
        raise InvalidTransition(f"A {pref.status} preference cannot become {status}.")
    if guidance is not None:
        set_guidance(pref, guidance, save=False)
    pref.status, pref.decided_by, pref.decided_at = status, user, timezone.now()
    pref.save()


def set_guidance(pref: HospitalPreference, guidance: str, *, save: bool = True) -> None:
    guidance = guidance.strip()
    if not guidance:
        raise InvalidEdit("guidance cannot be empty.")
    if find_banned(guidance):
        raise InvalidEdit("Medicines, supplements, doses and calorie or weight targets are not allowed.")
    pref.payload = {**pref.payload, "guidance": guidance}
    if save:
        pref.save(update_fields=["payload", "updated_at"])


# ── dashboard workflows (resolved by core.patients via importlib) ─────────


def _current_week_exists(**extra):
    today = timezone.localdate()
    return CareWeek.objects.filter(
        care_plan__pregnancy=OuterRef("pk"), week_start__lte=today, week_end__gte=today, **extra
    )


def patients_needing_care_plan_review(queryset):
    """This week's plan exists, nobody has reviewed it, and it is for a medium- or
    high-risk patient (low-risk weekly plans do not need a doctor)."""
    pending = Pregnancy.objects.filter(
        patient=OuterRef("pk"),
        status=Pregnancy.STATUS_ACTIVE,
    ).filter(
        Exists(
            _current_week_exists(
                care_plan__status=CarePlan.STATUS_IN_PROGRESS,
                care_plan__current_state__risk__in=["medium", "high"],
            )
        )
    )
    return queryset.filter(Exists(pending))


def patients_missing_care_plan(queryset):
    """Active pregnancy, but no weekly plan covering today (no readings yet, so
    nothing has written one)."""
    missing = Pregnancy.objects.filter(
        patient=OuterRef("pk"),
        status=Pregnancy.STATUS_ACTIVE,
    ).exclude(Exists(_current_week_exists()))
    return queryset.filter(Exists(missing))
