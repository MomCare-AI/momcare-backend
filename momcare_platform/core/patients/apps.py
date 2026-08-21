from django.apps import AppConfig


class PatientsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "momcare_platform.core.patients"
    label = "patients"

    def ready(self):
        import momcare_platform.core.patients.admin  # noqa: F401,PLC0415
