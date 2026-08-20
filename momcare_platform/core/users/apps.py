from django.apps import AppConfig


class UsersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "momcare_platform.core.users"
    label = "users"

    def ready(self):
        from momcare_platform.core.users import signals  # noqa: F401,PLC0415
