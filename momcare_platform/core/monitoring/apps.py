from django.apps import AppConfig


class MonitoringConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "momcare_platform.core.monitoring"
    label = "monitoring"

    def ready(self):
        import momcare_platform.core.monitoring.admin  # noqa: F401,PLC0415
