from django.apps import AppConfig


class OrganizationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "momcare_platform.core.organization"
    label = "organization"

    def ready(self):
        from momcare_platform.core.organization import signals  # noqa: F401,PLC0415
        import momcare_platform.core.organization.admin  # noqa: F401,PLC0415
