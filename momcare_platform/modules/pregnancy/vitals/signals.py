"""Automatic risk scoring whenever a reading is saved.

Reassessment used to be triggered by hand, called inline inside
``ReadingListCreateView.post()`` — correct today because that view is the
only place a reading is ever created, but a second write path added later
(a bulk import, a different endpoint) could easily forget that call and save
a reading with no risk check at all, silently. This signal closes that
door: any code that creates a ``VitalReading`` gets scored, regardless of
which code that is — the check no longer depends on one view remembering to
run it.

Fires synchronously, inside the same transaction as the save that triggered
it (Django dispatches ``post_save`` in-process, and ``ATOMIC_REQUESTS``
already wraps every request in one transaction) — this changes nothing
about the "scoring and alerting are one transaction" guarantee documented on
``reassess_risk()`` itself.

Does NOT fire for ``bulk_create()`` — Django signals never do, for any
model, by design. A future bulk-reading import must call ``reassess_risk()``
explicitly per pregnancy; this signal only covers the ordinary
``.create()``/``.save()`` path.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from momcare_platform.modules.pregnancy.vitals.models import VitalReading
from momcare_platform.modules.pregnancy.vitals.services import reassess_risk


@receiver(post_save, sender=VitalReading)
def score_reading_on_save(sender, instance, created, **kwargs):
    if not created:
        return
    reassess_risk(instance.pregnancy)
