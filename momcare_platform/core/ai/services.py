import importlib
import logging
from contextlib import nullcontext

from django.apps import apps as django_apps
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from momcare_platform.core.ai import openrouter_client
from momcare_platform.core.ai.models import AIInstructionPreset, AIProviderConfig, AISummary
from momcare_platform.core.analytics.models import PatientAnalytics
from momcare_platform.core.common.formatting import humanize_days_ago
from momcare_platform.core.common.rls import bypass_rls
from momcare_platform.core.monitoring.services import format_duration
from momcare_platform.core.patients.models import Patient

logger = logging.getLogger(__name__)


class InstructionPresetStateError(Exception):
    """Raised when an activate/deactivate call doesn't apply to the
    preset's current state -- e.g. activating one that's already active.
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


_BASE_PROMPT = """You are writing a short clinical summary for a hospital staff member about one pregnant patient. Use only the data given below -- never invent a value, a name, or an event that is not present. If something is missing (no care team assigned, no readings this period, no recent note), state that plainly instead of omitting it. Write in plain prose, no bullet points, no markdown. Keep the entire summary to at most {max_words} words.

Patient data:
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


def _build_prompt(
    snapshot: dict,
    config: AIProviderConfig,
    platform_instructions: str,
    org_instructions: str,
    *,
    deactivated: bool,
) -> str:
    data_lines = "\n".join(f"- {key}: {_format_snapshot_value(value)}" for key, value in snapshot.items())
    sections = [
        _BASE_PROMPT.format(
            max_words=config.max_words,
            data_lines=data_lines,
            closing_instruction=_DEACTIVATED_CLOSING if deactivated else _ACTIVE_CLOSING,
        ),
    ]
    if platform_instructions:
        sections.append(platform_instructions)
    if org_instructions:
        sections.append(org_instructions)
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
            platform_instructions = (
                AIInstructionPreset.objects.filter(organization__isnull=True, is_active=True)
                .values_list("content", flat=True)
                .first()
                or ""
            )
            org_instructions = (
                AIInstructionPreset.objects.filter(organization=patient.organization, is_active=True)
                .values_list("content", flat=True)
                .first()
                or ""
            )
            prompt = _build_prompt(
                snapshot,
                config,
                platform_instructions,
                org_instructions,
                deactivated=deactivated,
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


def activate_instruction_preset(preset: AIInstructionPreset) -> None:
    """At most one active preset per scope. ``organization=preset.organization``
    scopes the "deactivate the others" step correctly for both tiers,
    including the platform tier (organization=None matches only other
    organization=None rows -- NULL never matches NULL in a WHERE clause via
    ``=``, but Django's ORM ``filter(organization=None)`` compiles to
    ``organization_id IS NULL``, not ``= NULL``, so this works)."""
    if preset.is_active:
        raise InstructionPresetStateError("This preset is already active.")
    with transaction.atomic():
        AIInstructionPreset.objects.filter(
            organization=preset.organization,
            is_active=True,
        ).exclude(pk=preset.pk).update(is_active=False)
        preset.is_active = True
        preset.activated_at = timezone.now()
        preset.save(update_fields=["is_active", "activated_at", "updated_at"])


def deactivate_instruction_preset(preset: AIInstructionPreset) -> None:
    if not preset.is_active:
        raise InstructionPresetStateError("This preset is not active.")
    preset.is_active = False
    preset.save(update_fields=["is_active", "updated_at"])
