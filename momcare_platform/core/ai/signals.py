"""Two of the four ways an AI Summary generation can be triggered -- see
docs/design/2026-09-27-ai-summary-design.md's Triggers section. The other
two are core/ai/management/commands/refresh_ai_summaries.py (periodic) and a
direct call inside core.patients.services.deactivate_patient() (one-time,
core-to-core, no signal needed there).

There is deliberately no manual "regenerate" endpoint -- these triggers, plus
the periodic command, are the only paths that may call generate_patient_summary().
"""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from momcare_platform.core.patients.models import Patient

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Patient, dispatch_uid="ai_generate_summary_on_patient_creation")
def generate_summary_on_patient_creation(sender, instance, created, **kwargs):
    if not created:
        return
    from momcare_platform.core.ai.services import generate_patient_summary  # noqa: PLC0415

    generate_patient_summary(instance)


def on_risk_assessment_saved(sender, instance, created, **kwargs):
    """Connected manually in AiConfig.ready(), not via @receiver -- its
    `sender` (RiskAssessment) lives in modules.pregnancy.vitals, which core
    must never import statically. See apps.py for the apps.get_model()
    resolution this depends on."""
    from momcare_platform.core.ai.services import maybe_regenerate_for_risk_change  # noqa: PLC0415

    maybe_regenerate_for_risk_change(instance)
