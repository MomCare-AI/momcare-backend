from django.apps import AppConfig


class AlertsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "momcare_platform.modules.pregnancy.alerts"
    # label is unchanged from the app's former home under core/. Migration
    # history and table names (alerts_alert, alerts_alertevent) key off this
    # label, not the Python package path, so keeping it fixed makes this a
    # pure folder move — no new migration, no RLS policy to rewrite. Same
    # folder/label mismatch Neuro_RPM itself accepts for CarePlan, and for
    # the same reason: a package's disk location is a registration artifact,
    # not a claim about what the label means.
    label = "alerts"
