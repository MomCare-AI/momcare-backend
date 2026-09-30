"""One of the four ways an AI Summary generation can be triggered -- see
docs/design/2026-09-27-ai-summary-design.md's Triggers section. The other
three are core/ai/management/commands/refresh_ai_summaries.py (periodic) and
direct calls inside core.patients.services.onboard_patient() (enrollment)
and deactivate_patient() (deactivation) -- both core-to-core, no signal
needed there.

Enrollment is a direct call, not a Patient post_save signal, on purpose: a
post_save signal fires the instant Patient.objects.create() runs, which is
*before* onboard_patient() creates the pregnancy for a patient onboarded
with obstetric data -- so the very first summary would always describe her
as having no pregnancy at all. Calling generate_patient_summary() explicitly
at the end of onboard_patient(), after the pregnancy exists, avoids that.

There is deliberately no manual "regenerate" endpoint -- these triggers, plus
the periodic command, are the only paths that may call generate_patient_summary().
"""

import logging

logger = logging.getLogger(__name__)


def on_risk_assessment_saved(sender, instance, created, **kwargs):
    """Connected manually in AiConfig.ready(), not via @receiver -- its
    `sender` (RiskAssessment) lives in modules.pregnancy.vitals, which core
    must never import statically. See apps.py for the apps.get_model()
    resolution this depends on."""
    from momcare_platform.core.ai.services import regenerate_for_new_reading  # noqa: PLC0415

    regenerate_for_new_reading(instance)
