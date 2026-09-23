from django.apps import AppConfig


class LocationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "momcare_platform.core.locations"
    label = "locations"

    def ready(self):
        import momcare_platform.core.locations.admin  # noqa: F401,PLC0415
