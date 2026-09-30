from django.apps import AppConfig


class AiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "momcare_platform.core.ai"
    label = "ai"

    def ready(self):
        from django.apps import apps as django_apps  # noqa: PLC0415
        from django.db.models.signals import post_save  # noqa: PLC0415

        from momcare_platform.core.ai import signals  # noqa: PLC0415

        # RiskAssessment lives in modules.pregnancy.vitals, which core must
        # never import statically -- resolved via apps.get_model() instead,
        # the same escape hatch core.patients already uses for this exact
        # cross-boundary case (see CLAUDE.md's "core must not import
        # modules" section). Safe to call here: Django populates every app's
        # models before calling any app's ready().
        RiskAssessment = django_apps.get_model("monitoring", "RiskAssessment")
        post_save.connect(
            signals.on_risk_assessment_saved,
            sender=RiskAssessment,
            dispatch_uid="ai_new_reading_trigger",
        )
