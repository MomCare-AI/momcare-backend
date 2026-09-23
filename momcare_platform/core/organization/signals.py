"""Notification producers -- each one turns a real event elsewhere in the
system into a ``Notification`` row for the hospital it happened to, so
staff see it the next time they check the bell icon. No email, no polling
required of them: the row exists the instant the triggering event commits.
"""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from momcare_platform.core.organization.models import Notification
from momcare_platform.core.patients.models import PatientJoinRequest

logger = logging.getLogger(__name__)


@receiver(post_save, sender=PatientJoinRequest, dispatch_uid="notify_hospital_of_join_request")
def notify_hospital_of_join_request(sender, instance, created, **kwargs):
    """A woman asked to join a hospital -- tell that hospital."""
    if not created:
        return

    Notification.objects.create(
        organization=instance.organization,
        notification_type=Notification.TYPE_PATIENT_JOIN_REQUEST,
        message=f"{instance.user.get_full_name() or instance.user.email} has requested to join your hospital.",
        related_object_id=instance.id,
    )
    logger.info(
        "Notified organization %s of join request %s",
        instance.organization_id,
        instance.id,
    )
