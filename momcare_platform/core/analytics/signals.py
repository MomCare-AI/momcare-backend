"""Keeps ``PatientAnalytics``/``Patient.last_monitoring_contact_at`` correct
whenever a ``MonitoringSession`` or ``MonitoringNote`` is written or removed.

Lives here (the consumer app) rather than in ``core.monitoring`` (the
source), matching that app's own signals.py precedent -- Location's
copy-down signals for ClinicalTag/StatusLabel/NoteTemplate are likewise
owned by the app that needs the copy, not the app being observed.

Captures the pre-save value of ``recorded_at`` so an edit that moves a
record across a month boundary refreshes both the old and new month's rows
-- see ``services.recompute_monitoring_analytics``'s own docstring. A
combined session+note write (``core.monitoring.services.
create_combined_monitoring``) triggers two recomputes for the same
patient/period -- harmless, since recompute always reads from source data
rather than accumulating a delta.
"""

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from momcare_platform.core.analytics.services import recompute_monitoring_analytics
from momcare_platform.core.monitoring.models import MonitoringNote, MonitoringSession
from momcare_platform.core.patients.models import Patient


def _stash_old_recorded_at(sender, instance):
    if not instance.pk:
        instance._old_recorded_at = None
        return
    instance._old_recorded_at = sender.objects.filter(pk=instance.pk).values_list("recorded_at", flat=True).first()


@receiver(pre_save, sender=MonitoringSession, dispatch_uid="analytics_stash_old_session_recorded_at")
def stash_old_session_recorded_at(sender, instance, **kwargs):
    _stash_old_recorded_at(sender, instance)


@receiver(pre_save, sender=MonitoringNote, dispatch_uid="analytics_stash_old_note_recorded_at")
def stash_old_note_recorded_at(sender, instance, **kwargs):
    _stash_old_recorded_at(sender, instance)


def _recompute_for(instance):
    patient = Patient.objects.select_related("location").get(pk=instance.patient_id)
    moments = [instance.recorded_at, getattr(instance, "_old_recorded_at", None)]
    recompute_monitoring_analytics(patient, moments)


@receiver(post_save, sender=MonitoringSession, dispatch_uid="analytics_recompute_on_session_save")
def recompute_on_session_save(sender, instance, **kwargs):
    _recompute_for(instance)


@receiver(post_delete, sender=MonitoringSession, dispatch_uid="analytics_recompute_on_session_delete")
def recompute_on_session_delete(sender, instance, **kwargs):
    _recompute_for(instance)


@receiver(post_save, sender=MonitoringNote, dispatch_uid="analytics_recompute_on_note_save")
def recompute_on_note_save(sender, instance, **kwargs):
    _recompute_for(instance)


@receiver(post_delete, sender=MonitoringNote, dispatch_uid="analytics_recompute_on_note_delete")
def recompute_on_note_delete(sender, instance, **kwargs):
    _recompute_for(instance)
