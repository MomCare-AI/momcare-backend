import importlib

from django.apps import apps as django_apps
from django.conf import settings
from django.utils import timezone

from momcare_platform.core.ai.models import AIProviderConfig
from momcare_platform.core.analytics.models import PatientAnalytics
from momcare_platform.core.common.formatting import humanize_days_ago
from momcare_platform.core.monitoring.services import format_duration


def get_ai_config() -> AIProviderConfig:
    """The single read path for AIProviderConfig -- creates the row from
    settings on first call, since none exists yet in a fresh database."""
    config = AIProviderConfig.objects.first()
    if config is not None:
        return config
    return AIProviderConfig.objects.create(
        current_model=settings.MOMCARE_AI_SUMMARY_DEFAULT_MODEL,
        max_words=settings.MOMCARE_AI_SUMMARY_DEFAULT_MAX_WORDS,
    )


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
