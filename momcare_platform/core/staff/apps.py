from django.apps import AppConfig


class StaffConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "momcare_platform.core.staff"
    label = "staff"

    def ready(self):
        from momcare_platform.core.staff import signals  # noqa: F401,PLC0415
