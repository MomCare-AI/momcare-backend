from django.apps import AppConfig


class PatientsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "momcare_platform.core.patients"
    label = "patients"
