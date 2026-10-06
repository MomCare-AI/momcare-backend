from django.apps import AppConfig


class CarePlansConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "momcare_platform.modules.pregnancy.care_plans"
    label = "care_plans"

    def ready(self):
        from django.db.models.signals import post_save  # noqa: PLC0415

        from momcare_platform.modules.pregnancy.care_plans import signals  # noqa: PLC0415
        from momcare_platform.modules.pregnancy.vitals.models import RiskAssessment  # noqa: PLC0415

        # One RiskAssessment is written per reading, so this fires on every
        # reading -- the same hook core.ai uses for the AI Summary. This app
        # lives under modules/, so it may import RiskAssessment directly.
        post_save.connect(
            signals.on_risk_assessment_saved,
            sender=RiskAssessment,
            dispatch_uid="care_plan_new_reading_trigger",
        )
