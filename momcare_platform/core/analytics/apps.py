from django.apps import AppConfig


class AnalyticsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "momcare_platform.core.analytics"
    label = "analytics"

    def ready(self):
        from momcare_platform.core.analytics import signals  # noqa: F401,PLC0415
