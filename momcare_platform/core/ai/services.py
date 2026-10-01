import importlib
import json
import logging
import re
from contextlib import nullcontext
from decimal import Decimal

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
    "structured data in, free-form prose out" section).

    The underscore-prefixed keys (_latest_reading_id, _provider_id,
    _nurse_id, _care_manager_id) are never part of TEMPLATE_FIELD_VOCABULARY
    and never reach the prompt -- _format_group() only ever reads the field names in
    the default layout lists / TEMPLATE_FIELD_VOCABULARY, so these extra keys are invisible
    to both the AI and any template. They exist purely for _build_citations()
    to link a value the AI's finished text happens to mention back to the
    real record it came from, without ever asking the AI what it meant."""
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
        "_latest_reading_id": None,
        "thirty_day_average": {},
        "provider_name": pregnancy.provider.user.get_full_name() if pregnancy and pregnancy.provider else None,
        "_provider_id": pregnancy.provider_id if pregnancy else None,
        "nurse_name": pregnancy.nurse.user.get_full_name() if pregnancy and pregnancy.nurse else None,
        "_nurse_id": pregnancy.nurse_id if pregnancy else None,
        "care_manager_name": (
            pregnancy.care_manager.user.get_full_name() if pregnancy and pregnancy.care_manager else None
        ),
        "_care_manager_id": pregnancy.care_manager_id if pregnancy else None,
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
            snapshot["_latest_reading_id"] = latest_reading.id

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


def _natural_number_str(value) -> str:
    """Reading fields are DecimalField(decimal_places=2) -- the real stored
    value is Decimal('142.00'), but a model writes '142' in natural prose,
    never a padded '.00'. Normalizes to how a person (or the AI) actually
    writes it: '142' for a whole number, '99.1' for a fractional one.
    Matching only the raw Decimal string meant a citation almost never
    actually matched in practice -- caught in live end-to-end testing."""
    number = float(value)
    if number == int(number):
        return str(int(number))
    return str(number)


def _build_citations(snapshot: dict, content: str) -> list[dict]:
    """Links a value the AI's *finished* text happens to mention back to
    the real record it came from -- readings and the three care-team
    roles (provider/nurse/care_manager). Deliberately the reverse of
    asking the AI what it meant: every candidate string here comes from
    data we already trust (the same snapshot that built the prompt), and
    a candidate only becomes a citation if it's found, verbatim, in text
    the AI already produced. A value the AI never mentions (or phrases
    differently -- "185 over 115" instead of "185/115") simply produces no
    citation, never a wrong one: the failure mode of substring matching is
    a missed link, not a fabricated one.

    recent_note_author is deliberately excluded -- it's a User id (the
    monitoring note's ``added_by``), not a Staff id like the other three,
    and resolving one from the other isn't done here; mixing the two id
    types under one "staff" citation type would be worse than omitting it.

    Known limitation, accepted rather than engineered around: a short
    scalar value (e.g. a heart rate of 88) could coincidentally match an
    unrelated number elsewhere in the text. The citation still always
    points to a *real* reading -- never a fabricated one -- just possibly
    attached to a coincidental occurrence of that number rather than the
    one the AI meant.
    """
    citations: dict[str, dict] = {}

    reading_id = snapshot.get("_latest_reading_id")
    if reading_id is not None:
        readings = snapshot.get("latest_readings") or {}
        candidates = []
        systolic = readings.get("systolic_bp")
        diastolic = readings.get("diastolic_bp")
        if systolic is not None and diastolic is not None:
            candidates.append(f"{_natural_number_str(systolic)}/{_natural_number_str(diastolic)}")
            candidates.append(f"{systolic}/{diastolic}")
        for key in ("heart_rate", "body_temp_f", "blood_glucose", "hemoglobin"):
            value = readings.get(key)
            if value is not None:
                candidates.append(_natural_number_str(value))
                candidates.append(str(value))
        for text in candidates:
            if text in content and text not in citations:
                citations[text] = {"text": text, "type": "reading", "id": str(reading_id)}

    for name_key, id_key in (
        ("provider_name", "_provider_id"),
        ("nurse_name", "_nurse_id"),
        ("care_manager_name", "_care_manager_id"),
    ):
        name = snapshot.get(name_key)
        staff_id = snapshot.get(id_key)
        if name and staff_id is not None and name in content and name not in citations:
            citations[name] = {"text": name, "type": "staff", "id": str(staff_id)}

    return list(citations.values())


_BASE_PROMPT = """You are writing a short clinical summary for a hospital staff member about one pregnant patient, the way a clinician would summarize a chart out loud -- flowing paragraphs grouped by topic, never a list of facts read out one after another. Use only the data given below -- never invent a value, a name, or an event that is not present. If something is missing (no care team assigned, no readings this period, no recent note), state that plainly instead of omitting it. {structure_instruction} State every item listed under Patient data with its actual value -- for readings and averages give the numbers themselves, never just a comparison -- and keep the writing compact (short sentences, no filler, numbers written as digits) so all of it fits. No bullet points, no markdown. The entire summary must be at most {max_words} words.

{data_lines}

{closing_instruction}"""

_TEMPLATE_STRUCTURE = (
    "The platform administrator wrote the guidance below describing how this summary should be "
    "organized. Follow its order and emphasis, and make sure everything it asks for is covered, "
    "using the patient data given underneath it. Write natural flowing prose -- never copy the "
    "guidance itself."
)

_ACTIVE_CLOSING = "Close with exactly one recommendation, grounded specifically in the data above."
_DEACTIVATED_CLOSING = (
    "This patient has just been deactivated. Close the summary by stating that clearly, "
    "instead of a forward-looking recommendation -- recommending future monitoring for "
    "someone no longer being monitored would not make sense."
)


def _format_snapshot_value(value):
    """The base prompt tells the model to state a gap plainly rather than
    omit it -- so a field the caller left None/blank must show up as text
    saying so, not vanish from the prompt entirely.

    Also where the raw-Python-repr bug lived: latest_readings/
    thirty_day_average are dicts of Decimal objects, and this used to just
    return them as-is, which f-string interpolation then rendered as
    "{'systolic_bp': Decimal('142.00'), ...}" -- a real Python repr shown
    directly to the model. Nothing told it blood pressure is conventionally
    written as one "142/91" pair, so its own phrasing varied
    unpredictably call to call -- exactly why _build_citations() (which
    looks for that same "142/91" pair in the finished text) missed it more
    often than not. Caught live end-to-end testing."""
    if value in (None, "", [], {}):
        return "not on file"
    if isinstance(value, dict):
        return _format_dict_value(value)
    if isinstance(value, Decimal):
        return _natural_number_str(value)
    return value


def _format_dict_value(value: dict) -> str:
    """systolic_bp/diastolic_bp are combined into one "142/91" pair -- the
    same convention a clinician actually writes blood pressure in, and the
    same shape _build_citations() looks for, so the model is far more
    likely to naturally echo it back. Any other dict (e.g. risk_this_month,
    which nests further) just flattens its own key/value pairs the same
    recursive way -- still readable, never a raw repr.

    Both BP keys can be *present but None* (nullable fields, not merely
    absent) -- combining them requires both to actually have a value, not
    just both keys existing, or _natural_number_str(None) crashes. Caught
    live end-to-end testing. A None-valued field elsewhere is skipped
    entirely rather than printed as the literal word "None"."""
    parts = []
    remaining = dict(value)
    systolic = remaining.get("systolic_bp")
    diastolic = remaining.get("diastolic_bp")
    if systolic is not None and diastolic is not None:
        remaining.pop("systolic_bp")
        remaining.pop("diastolic_bp")
        parts.append(f"blood_pressure: {_natural_number_str(systolic)}/{_natural_number_str(diastolic)}")
    for key, val in remaining.items():
        if val is None:
            continue
        if isinstance(val, dict):
            parts.append(f"{key}: {_format_snapshot_value(val)}")
        elif isinstance(val, Decimal):
            parts.append(f"{key}: {_natural_number_str(val)}")
        else:
            parts.append(f"{key}: {val}")
    return ", ".join(parts) if parts else "not on file"


def _format_group(snapshot: dict, field_names: list[str]) -> str:
    return "\n".join(f"- {key}: {_format_snapshot_value(snapshot[key])}" for key in field_names)


def _resolve_active_template():
    """The platform's single active AISummaryTemplate, or None if an admin has
    deactivated every one (which deactivate_summary_template() refuses to do,
    so None only happens on a database that never had one). There is no layout
    in code any more: the default template is a real database row (seeded by
    migration 0014) that the platform admin can read and replace."""
    return AISummaryTemplate.objects.filter(organization__isnull=True, is_active=True).first()


def _build_prompt(
    snapshot: dict,
    config: AIProviderConfig,
    *,
    deactivated: bool,
    template: AISummaryTemplate,
) -> str:
    """The fixed safety rules and closing line (in code, never in a template)
    around the admin's own template text and the patient's data."""
    data_lines = (
        f"Administrator's guidance:\n{template.content.strip()}\n\n"
        f"Patient data:\n{_format_group(snapshot, list(TEMPLATE_FIELD_VOCABULARY))}"
    )
    return _BASE_PROMPT.format(
        max_words=config.max_words,
        structure_instruction=_TEMPLATE_STRUCTURE,
        data_lines=data_lines,
        closing_instruction=_DEACTIVATED_CLOSING if deactivated else _ACTIVE_CLOSING,
    )


def _max_tokens_for(max_words: int) -> int:
    """Generous backstop, not a length target -- roughly 2 tokens per word
    plus headroom, so it only catches a model that truly ignores the
    word-count instruction rather than shaping length on its own."""
    return max_words * 2 + 100


_GENERATE_MAX_ATTEMPTS = 3


def _is_usable_content(content: str | None) -> bool:
    return content is not None and content.strip() != ""


def generate_with_retries(prompt: str, *, model: str, max_tokens: int) -> str | None:
    """openrouter_client.generate() can come back None (a real transport or
    API failure) or, with a reasoning-style model, a non-None but blank
    string -- caught live testing this feature: the model spent its entire
    token budget on internal "reasoning" tokens (counted against the same
    max_tokens) and never wrote a visible answer at all. Both are equally
    unusable, but unlike a genuine transport failure, retrying a blank
    response often succeeds -- how much a reasoning model "thinks" before
    answering varies call to call for the identical prompt (measured live:
    4 of 5 real attempts against the summary prompt came back blank, the
    5th succeeded outright). Returns None only if every attempt failed."""
    for _attempt in range(_GENERATE_MAX_ATTEMPTS):
        content = openrouter_client.generate(prompt, model=model, max_tokens=max_tokens)
        if _is_usable_content(content):
            return content
    return None


_SHORTEN_PROMPT = """Rewrite the clinical summary below in at most {max_words} words (it is currently {current} words). Keep every value (readings, averages, counts, names, dates) and the final recommendation; remove only filler words and repetition. Keep the same paragraph breaks. Respond with ONLY the rewritten summary.

{content}"""

_SHORTEN_MAX_ATTEMPTS = 2


def _trim_to_word_limit(content: str, limit: int) -> str:
    """Last resort: cut at the limit, backing up to the end of the last
    complete sentence when one ends in the second half of the kept text."""
    ends = [m.end() for m in re.finditer(r"\S+", content)]
    if len(ends) <= limit:
        return content
    kept = content[: ends[limit - 1]]
    last_stop = max(kept.rfind("."), kept.rfind("!"), kept.rfind("?"))
    if last_stop >= len(kept) // 2:
        kept = kept[: last_stop + 1]
    return kept.rstrip()


def enforce_word_limit(content: str, config: AIProviderConfig) -> str:
    """The word limit applies to the summary a patient page actually shows --
    not just to the instruction given to the AI, which a model can exceed
    (measured on production: 160 words against a limit of 130). Over the
    limit, the AI is asked to shorten it (keeping every value); if it still
    won't fit after a couple of tries, the text is trimmed at a sentence
    boundary so the limit always holds."""
    limit = config.max_words
    for _attempt in range(_SHORTEN_MAX_ATTEMPTS):
        words = len(content.split())
        if words <= limit:
            return content
        shortened = generate_with_retries(
            _SHORTEN_PROMPT.format(max_words=limit, current=words, content=content),
            model=config.current_model,
            max_tokens=_max_tokens_for(limit),
        )
        if shortened is not None and len(shortened.split()) < words:
            content = shortened.strip()
    if len(content.split()) > limit:
        logger.warning("AI summary still over %s words after shortening; trimmed.", limit)
        content = _trim_to_word_limit(content, limit)
    return content


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
            template = _resolve_active_template()
            if template is None:
                logger.warning(
                    "No active AI summary template -- skipped generating a summary for patient %s. "
                    "Activate one under platform-admin AI config.",
                    patient.pk,
                )
                return
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
        content = generate_with_retries(
            prompt,
            model=config.current_model,
            max_tokens=_max_tokens_for(config.max_words),
        )
        if content is None:
            return
        content = enforce_word_limit(content, config)

        citations = _build_citations(snapshot, content)

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
                    "citations": citations,
                },
            )
    except Exception:
        logger.exception("generate_patient_summary() failed for patient %s", patient.pk)


def regenerate_for_new_reading(risk_assessment) -> None:
    """Connected to RiskAssessment's post_save in AiConfig.ready() (see
    apps.py) -- fires on every reading (reassess_risk() writes one row per
    reading, unconditionally), and regenerates the summary every time,
    regardless of whether the risk level actually moved.

    Reversed from the original "only regenerate if the level changed"
    design -- real usage showed a patient can have several consecutive
    readings at the same level (e.g. stays "high" across many readings),
    and every one of those is still new information (a new reading date,
    a new latest_readings value, a new monitoring-time total) that
    deserves to be reflected in what's shown. "only on level change" left
    the summary visibly stale on the patient overview screen even though
    real, newer readings had already come in.

    Skips a deactivated patient entirely: her summary was frozen on purpose
    by the deactivation trigger's own closing-line fork, and a late-arriving
    or backfilled reading must never silently unfreeze it with a
    forward-looking one.
    """
    patient_id = risk_assessment.pregnancy.patient_id
    if not Patient.objects.filter(pk=patient_id, is_active=True).exists():
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


# The fixed vocabulary _build_data_snapshot() produces -- the only facts a
# summary template may ever ask for. The platform admin writes the template in
# plain language; check_template_coverage() has the AI decide which of these
# the text covers, so a field added to the snapshot without being added here
# (and to TEMPLATE_FIELD_LABELS) would never be checked for.
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


# Plain-English description of each field: shown to the admin in "add these"
# alerts, and given to the AI so it knows what each topic means.
TEMPLATE_FIELD_LABELS = {
    "patient_name": "the patient's name",
    "gestational_age": "how far along the pregnancy is (gestational age)",
    "current_risk_level": "the current risk level (low / medium / high)",
    "risk_this_month": "the share of low / medium / high risk this month",
    "latest_readings": "the latest vital readings (blood pressure, heart rate, temperature, glucose, hemoglobin)",
    "thirty_day_average": "the 30-day average of the vitals",
    "provider_name": "the provider (doctor) name",
    "nurse_name": "the nurse's name",
    "care_manager_name": "the care manager's name",
    "recent_note": "the most recent clinical note",
    "recent_note_author": "who wrote the most recent note",
    "last_monitoring_contact_display": "when staff last contacted or monitored her",
    "last_reading_display": "when her last reading was received",
    "monitoring_time_display": "the monitoring time logged this month",
    "active_statuses": "her current statuses",
    "pending_risk_count": "how many risk assessments are waiting for review",
    "has_open_alert": "whether she has an open alert",
}


def template_word_count(content: str) -> int:
    return len(content.split())


def validate_template_content(content) -> list[str]:
    """The rules that need no AI: not blank, and no more words than the
    platform's configured limit. Whether every field is covered is a
    separate, AI-judged check -- see check_template_coverage(). Returns
    human-readable error strings; empty means valid. Never raises."""
    if not isinstance(content, str) or not content.strip():
        return ["Write the summary template -- it can't be empty."]
    limit = get_ai_config().max_words
    words = template_word_count(content)
    if words > limit:
        return [f"This template uses {words} words, but the limit is {limit}. Shorten it to {limit} or fewer."]
    return []


_COVERAGE_PROMPT = """A platform administrator wrote guidance describing what a patient summary should contain and in what order. Decide which of the following topics the guidance clearly asks to be included.

Topics (key: meaning):
{topic_list}

Rules:
- The administrator writes in plain words and may use their own phrasing, abbreviations or example values (for example "reading 120/80" asks for the latest readings). Judge by meaning, not exact wording.
- Mark a topic as covered ONLY if the guidance clearly asks for it. If you are unsure, leave it out.
- Do not mark a topic as covered just because a related one is.

The administrator's guidance:
{draft}

Respond with ONLY a JSON object of the form {{"covered": ["key", "key"]}} listing the keys of the covered topics. No commentary, no markdown."""

_COVERAGE_MAX_ATTEMPTS = 2


def _parse_covered(raw: str | None) -> set[str] | None:
    if raw is None:
        return None
    text = raw.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    covered = data.get("covered") if isinstance(data, dict) else None
    if not isinstance(covered, list):
        return None
    return {key for key in covered if key in TEMPLATE_FIELD_VOCABULARY}


def check_template_coverage(content: str) -> list[str] | None:
    """Which of the 17 fields the admin's plain-language text does NOT yet
    cover, judged by the AI. Returns [] when everything is covered, or None
    when the AI could not give a usable answer -- callers must treat None as
    "could not verify" and refuse to save, never as "covered" (fail closed,
    same reasoning as validating a model id against OpenRouter)."""
    config = get_ai_config()
    prompt = _COVERAGE_PROMPT.format(
        topic_list="\n".join(f"- {key}: {TEMPLATE_FIELD_LABELS[key]}" for key in TEMPLATE_FIELD_VOCABULARY),
        draft=content.strip(),
    )
    for _attempt in range(_COVERAGE_MAX_ATTEMPTS):
        raw = generate_with_retries(prompt, model=config.current_model, max_tokens=400)
        covered = _parse_covered(raw)
        if covered is not None:
            return [f for f in TEMPLATE_FIELD_VOCABULARY if f not in covered]
    return None


def missing_fields_message(missing: list[str]) -> str:
    return "Add the remaining details to your template: " + "; ".join(TEMPLATE_FIELD_LABELS[f] for f in missing) + "."


def activate_summary_template(template: AISummaryTemplate) -> None:
    """At most one active template, platform-wide. The advisory lock
    serializes two concurrent activations (see _lock_scope)."""
    if template.is_active:
        raise ActivationStateError("This template is already active.")
    with transaction.atomic():
        _lock_scope("summary_template", None)
        AISummaryTemplate.objects.filter(organization__isnull=True, is_active=True).exclude(pk=template.pk).update(
            is_active=False,
        )
        template.is_active = True
        template.activated_at = timezone.now()
        template.save(update_fields=["is_active", "activated_at", "updated_at"])


def deactivate_summary_template(template: AISummaryTemplate) -> None:
    """Never allowed to leave zero active templates: with none active, no
    patient summary can be generated. At most one template is ever active, so
    any active template IS the last one -- the only way to change which
    template is in use is to save or activate another, which switches this
    one off in the same step."""
    if not template.is_active:
        raise ActivationStateError("This template is not active.")
    raise ActivationStateError(
        "The active template can't be deactivated -- patients would be left without a summary. "
        "Write and save a new template, or activate another one, and this one switches off automatically.",
    )


# Fixed, made-up data -- never a real patient. Used only to render a preview
# of a candidate template during platform-admin authoring (no real PHI in
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


_ENHANCE_PROMPT = """You are helping a platform administrator polish the wording of the guidance they wrote for a clinical patient summary. The guidance says what the summary should contain and in what order.

Rules -- all are hard requirements, never break any:
- Fix grammar, spelling and clarity only. Preserve the administrator's meaning, order and emphasis.
- Keep everything the administrator asked to include. Never drop a topic, never add one they did not ask for.
- Never add any fact or instruction the administrator did not write.
- The draft is {draft_word_count} words. Stay close to that length, and never exceed {limit} words.

The administrator's draft:
{draft}

Respond with ONLY the improved text, no commentary, no quotation marks around it, no markdown."""


def _enhance_template_wording(content: str, config) -> str | None:
    """AI polish of the admin's own text. Only accepted if it still fits the
    word limit AND still covers all 17 fields (re-checked, since a polish that
    quietly dropped a topic must never be shown as safe); otherwise None."""
    draft = content.strip()
    prompt = _ENHANCE_PROMPT.format(
        limit=config.max_words,
        draft_word_count=template_word_count(draft),
        draft=draft,
    )
    enhanced = generate_with_retries(prompt, model=config.current_model, max_tokens=_max_tokens_for(config.max_words))
    if enhanced is None:
        return None
    enhanced = enhanced.strip()
    if not enhanced or template_word_count(enhanced) > config.max_words:
        return None
    if check_template_coverage(enhanced) != []:
        return None
    return enhanced


def review_summary_template(content: str) -> dict | None:
    """The review/"enhance" step of authoring a template. ``content`` is the
    admin's plain-language draft (word limit already checked by the caller).

    The AI first decides which of the 17 fields the text covers. If any are
    missing, nothing else is generated -- the admin gets back exactly what is
    still missing, in plain words, to add. Once everything is covered, the AI
    polishes the wording/grammar, and the entire summary is rendered exactly
    as a patient's page would show it, using fixed sample data (never a real
    patient). Returns None if the AI cannot verify coverage or the polish
    fails after retrying; a failed preview alone still returns the polished
    text with preview_text=None. Stateless: nothing is saved here."""
    missing = check_template_coverage(content)
    if missing is None:
        return None
    if missing:
        return {
            "complete": False,
            "missing_fields": missing,
            "missing_labels": [TEMPLATE_FIELD_LABELS[f] for f in missing],
            "message": missing_fields_message(missing),
            "enhanced_content": None,
            "preview_text": None,
        }

    config = get_ai_config()
    enhanced = _enhance_template_wording(content, config)
    if enhanced is None:
        return None

    preview_prompt = _build_prompt(
        _SAMPLE_SNAPSHOT,
        config,
        deactivated=False,
        template=AISummaryTemplate(content=enhanced),
    )
    preview_text = generate_with_retries(
        preview_prompt,
        model=config.current_model,
        max_tokens=_max_tokens_for(config.max_words),
    )
    if preview_text is not None:
        preview_text = enforce_word_limit(preview_text, config)
    return {
        "complete": True,
        "missing_fields": [],
        "missing_labels": [],
        "enhanced_content": enhanced,
        "word_count": template_word_count(enhanced),
        "word_limit": config.max_words,
        "preview_text": preview_text,
    }
