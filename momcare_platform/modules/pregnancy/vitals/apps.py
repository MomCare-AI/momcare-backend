from django.apps import AppConfig


class VitalsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "momcare_platform.modules.pregnancy.vitals"
    # label deliberately kept as "monitoring", NOT renamed to "vitals": all 15
    # existing migrations' internal dependency chains, the table names
    # (monitoring_device, monitoring_vitalreading, monitoring_riskassessment),
    # and the RLS policies on those tables all key off this label. Changing
    # it would turn a zero-risk folder move into a schema migration. The
    # folder/label mismatch is intentional and precedented — Neuro_RPM
    # accepts the same thing for CarePlan (label rpm_care_plans, meaning
    # unrelated to the RPM program). `core/monitoring` the NAME is what this
    # move frees up, for MonitoringSession/MonitoringNote — this app's
    # internal identity doesn't need to follow.
    label = "monitoring"

    def ready(self):
        import momcare_platform.modules.pregnancy.vitals.admin  # noqa: F401,PLC0415
        import momcare_platform.modules.pregnancy.vitals.signals  # noqa: F401,PLC0415
