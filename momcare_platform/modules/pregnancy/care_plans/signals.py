"""Connected manually in CarePlansConfig.ready() -- see apps.py."""

import logging

logger = logging.getLogger(__name__)


def on_risk_assessment_saved(sender, instance, created, **kwargs):
    """Every reading writes one RiskAssessment, so this runs once per reading.

    Imported lazily so loading the app registry never imports the generator
    (and through it the AI client) before every app is ready.
    """
    from momcare_platform.modules.pregnancy.care_plans.services import schedule_for_assessment  # noqa: PLC0415

    schedule_for_assessment(instance)
