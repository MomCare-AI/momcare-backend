"""Org-to-location propagation for ClinicalTag and StatusLabel.

Same shape as Neuro_RPM's own org->location copy signals
(``organization.signals.copy_org_note_templates_to_new_location`` and its
two siblings) applied to models Neuro_RPM itself never scoped this way --
its ``ClinicalTag``/``GlobalStatus`` have no organization/location at all
(``GlobalStatus`` does, but is single-tenant single-org so it copies via a
plain service-function call, not a signal), because Neuro_RPM is
single-tenant. MomCare is shared-schema multi-tenant with multi-location
hospitals, so the same "org-level entries seed every new location, then the
two sides are independent" pattern applies here too.
"""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from momcare_platform.core.locations.models import Location
from momcare_platform.core.monitoring.models import ClinicalTag, StatusLabel

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Location, dispatch_uid="copy_org_clinical_tags_to_new_location")
def copy_org_clinical_tags_to_new_location(sender, instance, created, **kwargs):
    """When a new Location is created, give it its own copy of every
    organization-level ClinicalTag for that location's hospital.

    Independent rows, not a shared reference -- renaming or recoloring the
    location's copy afterward never touches the organization's original,
    and vice versa.
    """
    if not created:
        return

    org_tags = ClinicalTag.objects.filter(organization=instance.organization, location__isnull=True)

    copied = 0
    for tag in org_tags:
        ClinicalTag.objects.create(
            name=tag.name,
            color=tag.color,
            location=instance,
            organization=None,
        )
        copied += 1

    if copied:
        logger.info(
            "Auto-copied %d organization clinical tags to new location %s",
            copied,
            instance.id,
        )


@receiver(post_save, sender=Location, dispatch_uid="copy_org_status_labels_to_new_location")
def copy_org_status_labels_to_new_location(sender, instance, created, **kwargs):
    """Same as ``copy_org_clinical_tags_to_new_location`` above, for
    StatusLabel -- fork-once at creation, independent rows after."""
    if not created:
        return

    org_labels = StatusLabel.objects.filter(organization=instance.organization, location__isnull=True)

    copied = 0
    for label in org_labels:
        StatusLabel.objects.create(
            name=label.name,
            description=label.description,
            color=label.color,
            location=instance,
            organization=None,
        )
        copied += 1

    if copied:
        logger.info(
            "Auto-copied %d organization status labels to new location %s",
            copied,
            instance.id,
        )
