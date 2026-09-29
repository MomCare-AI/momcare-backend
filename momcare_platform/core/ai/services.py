import importlib
import json
import logging
from contextlib import nullcontext

from django.apps import apps as django_apps
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from momcare_platform.core.ai import openrouter_client
from momcare_platform.core.ai.models import AIProviderConfig, AISummary, AISummaryTemplate
from momcare_platform.core.analytics.models import PatientAnalytics
from momcare_platform.core.common.formatting import humanize_days_ago
from momcare_platform.core.common.rls import bypass_rls
from momcare_platform.core.monitoring.services import format_duration
from momcare_platform.core.patients.models import Patient

logger = logging.getLogger(__name__)


class ActivationStateError(Exception):
    """Raised when an activate/deactivate call doesn't apply to the
    resource's current state -- e.g. activating one that's already active.
    A 400 at the view layer, never a silent no-op that could mask a
    caller bug like a double-click racing itself."""


def get_ai_config() -> AIProviderConfig:
    """The single read path for AIProviderConfig -- creates the row from
    settings on first call, since none exists yet in a fresh database.

    get_or_create() keyed on the fixed singleton_id (rather than .first() +
    .create()) so two concurrent first-callers can't each see zero rows and
    each create their own -- the second one's INSERT hits the unique
    constraint and get_or_create() re-queries instead of raising.
    """
    config, _ = AIProviderConfig.objects.get_or_create(
        singleton_id=1,
        defaults={
            "current_model": settings.MOMCARE_AI_SUMMARY_DEFAULT_MODEL,
            "max_words": settings.MOMCARE_AI_SUMMARY_DEFAULT_MAX_WORDS,
        },
    )
    return config


def _build_data_snapshot(patient) -> dict:
    """Everything the prompt builder needs, gathered once. No narrative
    judgement happens here -- that's the model's job (see the design doc's
    "structured data in, free-form prose out" section)."""
    RiskAssessment = django_apps.get_model("monitoring", "RiskAssessment")
    VitalReading = django_apps.get_model("monitoring", "VitalReading")
    Alert = django_apps.get_model("alerts", "Alert")
    vitals_services = importlib.import_module("momcare_platform.modules.pregnancy.vitals.services")

    pregnancy = patient.current_pregnancy

    snapshot: dict = {
        "patient_name": patient.full_name,
        "gestational_age": pregnancy.gestational_age_long_display if pregnancy else None,
        "current_risk_level": None,
        "risk_this_month": None,
        "latest_readings": {},
        "thirty_day_average": {},
        "provider_name": pregnancy.provider.user.get_full_name() if pregnancy and pregnancy.provider else None,
        "nurse_name": pregnancy.nurse.user.get_full_name() if pregnancy and pregnancy.nurse else None,
        "care_manager_name": (
            pregnancy.care_manager.user.get_full_name() if pregnancy and pregnancy.care_manager else None
        ),
        "recent_note": None,
        "recent_note_author": None,
        "last_monitoring_contact_display": humanize_days_ago(
            patient.last_monitoring_contact_at,
            patient.location.timezone,
        ),
        "last_reading_display": humanize_days_ago(patient.last_reading_at, patient.location.timezone),
        "monitoring_time_display": None,
        "active_statuses": list(patient.statuses.order_by("-created_at").values_list("name", flat=True)[:5]),
        "pending_risk_count": 0,
        "has_open_alert": False,
    }

    if pregnancy is not None:
        latest_assessment = RiskAssessment.objects.filter(pregnancy=pregnancy).order_by("-assessed_at").first()
        if latest_assessment is not None:
            snapshot["current_risk_level"] = latest_assessment.final_risk_level

        vitals_summary = vitals_services.compute_vitals_summary(pregnancy)
        snapshot["thirty_day_average"] = vitals_summary["last_30_days_average"]
        snapshot["risk_this_month"] = vitals_summary["risk_this_month"]

        latest_reading = VitalReading.objects.filter(pregnancy=pregnancy).order_by("-recorded_at").first()
        if latest_reading is not None:
            snapshot["latest_readings"] = {
                "systolic_bp": latest_reading.systolic_bp,
                "diastolic_bp": latest_reading.diastolic_bp,
                "heart_rate": latest_reading.heart_rate,
                "body_temp_f": latest_reading.body_temp_f,
                "blood_glucose": latest_reading.blood_glucose,
                "hemoglobin": latest_reading.hemoglobin,
            }

        pending = RiskAssessment.objects.filter(pregnancy=pregnancy, review_status=RiskAssessment.REVIEW_PENDING)
        snapshot["pending_risk_count"] = sum(1 for assessment in pending if assessment.needs_attention)

        snapshot["has_open_alert"] = Alert.objects.filter(pregnancy=pregnancy, status=Alert.STATUS_OPEN).exists()

    note = patient.monitoring_notes.order_by("-recorded_at").first()
    if note is not None:
        snapshot["recent_note"] = note.note
        snapshot["recent_note_author"] = note.added_by.full_name if note.added_by else None

    month_start = timezone.now().date().replace(day=1)
    analytics_row = PatientAnalytics.objects.filter(patient=patient, period_month=month_start).first()
    if analytics_row is not None:
        snapshot["monitoring_time_display"] = format_duration(analytics_row.monitoring_seconds)

    return snapshot


_BASE_PROMPT = """You are writing a short clinical summary for a hospital staff member about one pregnant patient, the way a clinician would summarize a chart out loud -- flowing paragraphs grouped by topic, never a list of facts read out one after another. Use only the data given below -- never invent a value, a name, or an event that is not present. If something is missing (no care team assigned, no readings this period, no recent note), state that plainly instead of omitting it. Write one paragraph synthesizing the Vitals & Risk section, then a separate paragraph covering the Care Team & Activity section. No bullet points, no markdown. Keep the entire summary to at most {max_words} words.

{data_lines}

{closing_instruction}"""

_ACTIVE_CLOSING = "Close with exactly one recommendation, grounded specifically in the data above."
_DEACTIVATED_CLOSING = (
    "This patient has just been deactivated. Close the summary by stating that clearly, "
    "instead of a forward-looking recommendation -- recommending future monitoring for "
    "someone no longer being monitored would not make sense."
)


def _format_snapshot_value(value):
    """The base prompt tells the model to state a gap plainly rather than
    omit it -- so a field the caller left None/blank must show up as text
    saying so, not vanish from the prompt entirely."""
    if value in (None, "", [], {}):
        return "not on file"
    return value


_VITALS_AND_RISK_FIELDS = [
    "gestational_age",
    "current_risk_level",
    "risk_this_month",
    "latest_readings",
    "thirty_day_average",
    "pending_risk_count",
    "has_open_alert",
]
_CARE_TEAM_AND_ACTIVITY_FIELDS = [
    "provider_name",
    "nurse_name",
    "care_manager_name",
    "recent_note",
    "recent_note_author",
    "last_monitoring_contact_display",
    "last_reading_display",
    "monitoring_time_display",
    "active_statuses",
]


def _format_group(snapshot: dict, field_names: list[str]) -> str:
    return "\n".join(f"- {key}: {_format_snapshot_value(snapshot[key])}" for key in field_names)


def _resolve_active_template(organization):
    """Precedence for which AISummaryTemplate shapes a summary's data_lines
    (and extra_instructions): the organization's own active template, else
    the platform's active template, else None (the caller falls back to the
    built-in default layout, no extra instructions). Deliberately picks
    exactly one source rather than merging -- two templates disagreeing
    about where a field belongs, or what extra wording applies, has no
    sensible merge, unlike the retired AIInstructionPreset system this
    replaced, which stacked platform + org text together."""
    org_template = AISummaryTemplate.objects.filter(organization=organization, is_active=True).first()
    if org_template is not None:
        return org_template
    return AISummaryTemplate.objects.filter(organization__isnull=True, is_active=True).first()


def _build_prompt(
    snapshot: dict,
    config: AIProviderConfig,
    *,
    deactivated: bool,
    template: AISummaryTemplate | None = None,
) -> str:
    if template is not None:
        data_lines = "\n\n".join(
            f"{section['label']}:\n{_format_group(snapshot, section['fields'])}" for section in template.sections
        )
    else:
        data_lines = (
            f"Patient: {snapshot['patient_name']}\n\n"
            f"Vitals & Risk:\n{_format_group(snapshot, _VITALS_AND_RISK_FIELDS)}\n\n"
            f"Care Team & Activity:\n{_format_group(snapshot, _CARE_TEAM_AND_ACTIVITY_FIELDS)}"
        )
    sections = [
        _BASE_PROMPT.format(
            max_words=config.max_words,
            data_lines=data_lines,
            closing_instruction=_DEACTIVATED_CLOSING if deactivated else _ACTIVE_CLOSING,
        ),
    ]
    extra_instructions = template.extra_instructions if template is not None else ""
    if extra_instructions:
        sections.append(extra_instructions)
    return "\n\n".join(sections)


def _max_tokens_for(max_words: int) -> int:
    """Generous backstop, not a length target -- roughly 2 tokens per word
    plus headroom, so it only catches a model that truly ignores the
    word-count instruction rather than shaping length on its own."""
    return max_words * 2 + 100


def generate_patient_summary(patient, *, deactivated: bool = False, use_rls_bypass: bool = False) -> None:
    """The single entry point every trigger (patient creation, a risk-level
    change, deactivation, the periodic refresh command) calls. Best-effort:
    on any failure -- building the snapshot, the client call, or the write --
    the existing cached AISummary row, if any, is left untouched and the
    failure is logged, never raised, matching core.common.mail's convention.
    A clinical request (recording a reading, onboarding a patient) must
    never fail because its incidental AI summary refresh did.

    ``use_rls_bypass`` is for refresh_ai_summaries only: a management command
    has no per-request Organization set, so it needs its own short-lived
    ``bypass_rls()`` scope around each of the two DB phases below -- never
    spanning the whole sweep (which would hold one transaction, and its
    locks, open for the entire multi-patient run), and never spanning the
    client call in between (see the no-bypass case's own comment).
    """
    read_scope = bypass_rls if use_rls_bypass else nullcontext
    write_scope = bypass_rls if use_rls_bypass else transaction.atomic
    try:
        with read_scope():
            snapshot = _build_data_snapshot(patient)
            config = get_ai_config()
            template = _resolve_active_template(patient.organization)
            prompt = _build_prompt(
                snapshot,
                config,
                deactivated=deactivated,
                template=template,
            )

        # No transaction or row lock is held across this call -- it is the
        # one third-party network request in this whole path, and holding a
        # Postgres lock for its duration (up to the client's own timeout)
        # would block any other request touching this same patient row for
        # just as long. See docs/design/2026-09-27-ai-summary-design.md's
        # Concurrency note: the guard belongs around the upsert, not the call.
        content = openrouter_client.generate(
            prompt,
            model=config.current_model,
            max_tokens=_max_tokens_for(config.max_words),
        )
        if content is None:
            return

        with write_scope():
            # Locks this patient's row only for the upsert itself, so a cron
            # sweep and a risk-level-change signal landing at the same moment
            # cannot both write at once and race each other's results.
            Patient.objects.select_for_update().get(pk=patient.pk)
            AISummary.objects.update_or_create(
                patient=patient,
                defaults={
                    "content": content,
                    "generated_at": timezone.now(),
                    "model_used": config.current_model,
                    "risk_level_at_generation": snapshot["current_risk_level"] or "",
                },
            )
    except Exception:
        logger.exception("generate_patient_summary() failed for patient %s", patient.pk)


def maybe_regenerate_for_risk_change(risk_assessment) -> None:
    """Connected to RiskAssessment's post_save in AiConfig.ready() (see
    apps.py) -- fires on every reading (reassess_risk() writes one row per
    reading, unconditionally), but only actually regenerates when the level
    genuinely moved since the cached summary was written. Most routine
    readings don't change the risk level, so this stays cheap.

    Reads the stored level via a plain queryset lookup keyed on patient_id,
    not `patient.ai_summary` -- a reverse OneToOne accessor Django caches on
    the Patient instance the first time it's read (including implicitly,
    e.g. inside AISummary.objects.update_or_create(patient=patient, ...)).
    Any later out-of-band write to that same AISummary row (a queryset
    .update(), or a second generate_patient_summary() call reached through a
    different Patient object earlier in the same request) never invalidates
    that cache, so reading through it here could silently compare against a
    stale value.

    Skips a deactivated patient entirely: her summary was frozen on purpose
    by the deactivation trigger's own closing-line fork, and a late-arriving
    or backfilled reading must never silently unfreeze it with a
    forward-looking one.
    """
    patient_id = risk_assessment.pregnancy.patient_id
    if not Patient.objects.filter(pk=patient_id, is_active=True).exists():
        return
    stored_level = (
        AISummary.objects.filter(patient_id=patient_id)
        .values_list(
            "risk_level_at_generation",
            flat=True,
        )
        .first()
    )
    if stored_level == risk_assessment.final_risk_level:
        return
    generate_patient_summary(risk_assessment.pregnancy.patient)


def _lock_scope(resource_kind: str, organization_id) -> None:
    """Serializes concurrent activate_*() calls for the same (resource kind,
    scope) pair. transaction.atomic() alone gives atomicity, not
    serializability: under READ COMMITTED, two concurrent activations of two
    DIFFERENT, not-yet-active rows in the same scope can both commit, each
    leaving its own row active -- neither activation's "deactivate the
    others" UPDATE touches the other's row, since neither is active yet,
    so there is no shared row for the two transactions to contend over
    without an explicit lock. hashtext() collapses the scope key (which
    resource kind, plus an org uuid or a fixed sentinel string for the
    platform tier) into a lockable bigint; pg_advisory_xact_lock (not
    pg_advisory_lock) releases automatically at transaction end, so a crash
    or an exception can't leave the scope locked forever. ``resource_kind``
    is kept as a parameter (rather than hardcoded) even though
    "summary_template" is the only caller today, so a future second
    activatable resource type can't collide with this one's lock keys."""
    scope_key = f"{resource_kind}:{organization_id or 'platform'}"
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [scope_key])


# The fixed vocabulary _build_data_snapshot() produces -- the only field
# names a AISummaryTemplate's sections may ever reference. Keeping this list
# here, next to _build_data_snapshot itself, is what makes "a template can
# rearrange fields, never invent or hide one" an enforced fact rather than a
# convention: validate_template_sections() and _build_prompt() both read
# from this single list, so a field added to the snapshot without being
# added here would fail template validation loudly, not silently.
TEMPLATE_FIELD_VOCABULARY = (
    "patient_name",
    "gestational_age",
    "current_risk_level",
    "risk_this_month",
    "latest_readings",
    "thirty_day_average",
    "provider_name",
    "nurse_name",
    "care_manager_name",
    "recent_note",
    "recent_note_author",
    "last_monitoring_contact_display",
    "last_reading_display",
    "monitoring_time_display",
    "active_statuses",
    "pending_risk_count",
    "has_open_alert",
)


def validate_template_sections(sections) -> list[str]:
    """Every field in TEMPLATE_FIELD_VOCABULARY must appear exactly once
    across all sections -- nothing less (a template can't hide a field by
    omitting it), nothing more (a template can't reference a field that
    doesn't exist). Returns a list of human-readable error strings; an empty
    list means the sections are valid. Never raises -- the caller (the
    serializer) decides what an error list means for the response."""
    errors: list[str] = []
    if not isinstance(sections, list) or not sections:
        return ["sections must be a non-empty list of {label, fields} objects."]

    seen: list[str] = []
    for index, section in enumerate(sections):
        if not isinstance(section, dict) or "label" not in section or "fields" not in section:
            errors.append(f"Section {index} must have a 'label' and a 'fields' list.")
            continue
        if not isinstance(section["label"], str) or not section["label"].strip():
            errors.append(f"Section {index} must have a non-empty 'label'.")
        for field in section["fields"]:
            if field not in TEMPLATE_FIELD_VOCABULARY:
                errors.append(f"'{field}' is not a known field.")
            else:
                seen.append(field)

    missing = [f for f in TEMPLATE_FIELD_VOCABULARY if f not in seen]
    for field in missing:
        errors.append(f"'{field}' is missing -- every field must appear somewhere.")

    duplicated = {f for f in seen if seen.count(f) > 1}
    for field in sorted(duplicated):
        errors.append(f"'{field}' appears more than once -- each field may appear exactly once.")

    return errors


def activate_summary_template(template: AISummaryTemplate) -> None:
    """At most one active template per scope. ``organization=template.organization``
    scopes the "deactivate the others" step correctly for both tiers,
    including the platform tier (organization=None matches only other
    organization=None rows -- NULL never matches NULL in a WHERE clause via
    ``=``, but Django's ORM ``filter(organization=None)`` compiles to
    ``organization_id IS NULL``, not ``= NULL``, so this works)."""
    if template.is_active:
        raise ActivationStateError("This template is already active.")
    with transaction.atomic():
        _lock_scope("summary_template", template.organization_id)
        AISummaryTemplate.objects.filter(
            organization=template.organization,
            is_active=True,
        ).exclude(pk=template.pk).update(is_active=False)
        template.is_active = True
        template.activated_at = timezone.now()
        template.save(update_fields=["is_active", "activated_at", "updated_at"])


def deactivate_summary_template(template: AISummaryTemplate) -> None:
    if not template.is_active:
        raise ActivationStateError("This template is not active.")
    template.is_active = False
    template.save(update_fields=["is_active", "updated_at"])


# Fixed, made-up data -- never a real patient. Used only to render a preview
# of a candidate template during AI-assisted authoring, at both tiers (the
# platform tier has no single hospital's patient to reach for anyway; using
# sample data at both keeps the mechanism identical and keeps no real PHI in
# what is fundamentally a design/testing tool). Shape matches
# _build_data_snapshot()'s output exactly -- every TEMPLATE_FIELD_VOCABULARY
# key is present.
_SAMPLE_SNAPSHOT = {
    "patient_name": "Jane Sample",
    "gestational_age": "6 months 2 weeks",
    "current_risk_level": "Medium",
    "risk_this_month": {"low": 40, "medium": 40, "high": 20},
    "latest_readings": {
        "systolic_bp": 128,
        "diastolic_bp": 84,
        "heart_rate": 88,
        "body_temp_f": 98.6,
        "blood_glucose": 95,
        "hemoglobin": 11.2,
    },
    "thirty_day_average": {
        "systolic_bp": 124,
        "diastolic_bp": 80,
        "heart_rate": 82,
        "body_temp_f": 98.4,
        "blood_glucose": 92,
        "hemoglobin": 11.5,
    },
    "provider_name": "Dr. Sample Provider",
    "nurse_name": "Sample Nurse",
    "care_manager_name": "Sample Care Manager",
    "recent_note": "Patient reports mild swelling in ankles.",
    "recent_note_author": "Sample Nurse",
    "last_monitoring_contact_display": "2 days ago",
    "last_reading_display": "Today",
    "monitoring_time_display": "24m 10s",
    "active_statuses": ["Stable"],
    "pending_risk_count": 1,
    "has_open_alert": False,
}

_PROPOSE_PROMPT = """You are helping a hospital administrator design the structure of an AI-generated clinical summary. The summary always covers the same fixed set of data fields -- you may only use fields from this exact list, and every one of the {field_count} fields must appear in your answer exactly once. Never invent a field, never omit one.

Fields:
{field_list}

The administrator's request: "{description}"

Respond with ONLY a JSON object, no markdown formatting, no commentary before or after it, in exactly this shape:
{{"sections": [{{"label": "<section name>", "fields": ["<field>", ...]}}, ...], "extra_instructions": "<a short sentence of extra guidance implied by the request, or an empty string if none>"}}"""

_PROPOSE_MAX_ATTEMPTS = 3
_PROPOSE_MAX_TOKENS = 800


def _extract_json_object(raw: str) -> str | None:
    """Models asked for "JSON only" still sometimes wrap it in a markdown
    code fence or add a stray sentence around it -- taking the substring
    between the first '{' and the last '}' is a simple, robust way to pull
    the object out regardless."""
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    return raw[start : end + 1]


def _parse_propose_response(raw: str) -> dict | None:
    """Never raises -- a malformed or unexpected response is just a
    signal to retry, not a caller-facing error."""
    extracted = _extract_json_object(raw)
    if extracted is None:
        return None
    try:
        data = json.loads(extracted)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "sections" not in data:
        return None
    extra_instructions = data.get("extra_instructions", "")
    if not isinstance(extra_instructions, str):
        extra_instructions = ""
    return {"sections": data["sections"], "extra_instructions": extra_instructions}


def propose_summary_template(description: str) -> dict | None:
    """Asks the AI to arrange the fixed field vocabulary (and optionally
    propose extra wording) from a plain-English description -- the
    AI-assisted step of authoring a template. Every candidate is validated
    through the exact same validate_template_sections() check a hand-built
    template gets; an invalid one (missing/duplicated/unknown field, or a
    response that isn't parseable JSON at all) is retried up to
    _PROPOSE_MAX_ATTEMPTS times before giving up. Returns None if the
    client call itself fails (no point retrying a transport-level failure)
    or if every attempt produced an invalid candidate. Nothing is ever
    saved here -- this only ever produces a draft for the caller to
    preview and, if they choose, save through the ordinary create
    endpoint."""
    config = get_ai_config()
    prompt = _PROPOSE_PROMPT.format(
        field_count=len(TEMPLATE_FIELD_VOCABULARY),
        field_list="\n".join(f"- {field}" for field in TEMPLATE_FIELD_VOCABULARY),
        description=description,
    )
    for _attempt in range(_PROPOSE_MAX_ATTEMPTS):
        raw = openrouter_client.generate(prompt, model=config.current_model, max_tokens=_PROPOSE_MAX_TOKENS)
        if raw is None:
            return None
        candidate = _parse_propose_response(raw)
        if candidate is None:
            continue
        if not validate_template_sections(candidate["sections"]):
            return candidate
    return None


def propose_and_preview_template(description: str) -> dict | None:
    """propose_summary_template() plus a real preview, generated the same
    way an actual summary would be, using fixed sample data and the
    candidate's own layout and wording. Returns None only when the
    proposal itself fails -- if the proposal succeeds but the preview
    call fails, the candidate is still returned with preview_text=None
    rather than discarding a valid, already-produced candidate over a
    second, independent transient failure."""
    candidate = propose_summary_template(description)
    if candidate is None:
        return None

    config = get_ai_config()
    draft_template = AISummaryTemplate(
        sections=candidate["sections"],
        extra_instructions=candidate["extra_instructions"],
    )
    preview_prompt = _build_prompt(_SAMPLE_SNAPSHOT, config, deactivated=False, template=draft_template)
    preview_text = openrouter_client.generate(
        preview_prompt,
        model=config.current_model,
        max_tokens=_max_tokens_for(config.max_words),
    )
    return {
        "sections": candidate["sections"],
        "extra_instructions": candidate["extra_instructions"],
        "preview_text": preview_text,
    }
