from django.apps import AppConfig


class MonitoringConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "momcare_platform.core.monitoring"
    # NOT "monitoring" -- modules.pregnancy.vitals already owns that label
    # (kept deliberately mismatched from its own folder name when it moved out
    # of core/, to preserve its table names and RLS policies -- see its own
    # apps.py). This app holds MonitoringSession/MonitoringNote/ClinicalTag,
    # the content core/monitoring's *name* was freed for; its own app_label
    # doesn't need to match either, so it picks one that's actually free.
    label = "clinical_notes"

    def ready(self):
        import momcare_platform.core.monitoring.admin  # noqa: F401,PLC0415
        from momcare_platform.core.monitoring import signals  # noqa: F401,PLC0415
