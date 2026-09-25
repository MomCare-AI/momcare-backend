"""Monitoring business logic: tenant-scoped tag get-or-create, atomic
combined session+note creation, and calendar-month duration totals. All
writes that touch more than one model go through here so the rules are
enforced in one place, transactionally -- same shape as Neuro_RPM's own
``core.monitoring.services``.
"""

from __future__ import annotations

import calendar

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework import serializers

from momcare_platform.core.monitoring.models import ClinicalTag, MonitoringNote, MonitoringSession


def _tag_scope(location):
    """Tags visible to staff at ``location``: their hospital's org-level
    tags, plus their own location's tags. Never another location's, and
    never another hospital's -- this is also what stops a client attaching
    another tenant's tag by guessing its id.
    """
    return ClinicalTag.objects.filter(
        Q(organization_id=location.organization_id, location__isnull=True) | Q(location=location),
    )


def get_or_create_tags(tag_specs: list[dict], *, location) -> list[ClinicalTag]:
    """Resolve a list of ``{"id": uuid | None, "name": str, "color": str | None}``
    dicts to ``ClinicalTag`` rows, scoped to ``location``'s hospital.

    If ``id`` is given, the existing tag is fetched directly from the
    accessible scope only (org-level tags of this hospital, or this
    location's own tags) -- ``name``/``color`` are ignored in that case,
    and an id outside that scope is rejected rather than silently
    attaching another tenant's or another location's tag. If ``id`` is
    absent, resolution falls back to name matching.

    Matching on name is case-insensitive and whitespace-trimmed. If a name
    already exists in scope, the existing tag is reused and the caller's
    ``color`` is ignored -- a shared tag is never silently repainted by a
    get-or-create call. If a name is new, it is created as a **location**-
    scoped tag (ad-hoc tags typed inline belong to the location that typed
    them; a hospital-wide tag is a deliberate admin action through the
    dedicated ClinicalTag endpoint, not an inline side effect).
    """
    scope = _tag_scope(location)
    tags: list[ClinicalTag] = []
    seen_ids: set = set()
    for spec in tag_specs:
        tag_id = spec.get("id")

        if tag_id:
            try:
                tag = scope.get(pk=tag_id)
            except ClinicalTag.DoesNotExist as err:
                raise serializers.ValidationError(f"Tag with id {tag_id} does not exist.") from err
            if tag.id not in seen_ids:
                tags.append(tag)
                seen_ids.add(tag.id)
            continue

        cleaned = spec.get("name", "").strip()
        if not cleaned:
            continue
        tag = scope.filter(name__iexact=cleaned).first()
        if tag is None:
            try:
                with transaction.atomic():
                    tag = ClinicalTag.objects.create(name=cleaned, color=spec.get("color"), location=location)
            except IntegrityError:
                # Lost a race with a concurrent request creating the same tag --
                # the savepoint above rolled back cleanly, so just re-fetch it.
                tag = scope.get(name__iexact=cleaned)
        if tag.id not in seen_ids:
            tags.append(tag)
            seen_ids.add(tag.id)
    return tags


@transaction.atomic
def create_combined_monitoring(
    *,
    patient,
    duration_seconds: int | None,
    recorded_at,
    added_by,
    note_text: str = "",
    tags: list[dict] | None = None,
    left_voicemail: bool = False,
    two_way_communication: bool = False,
) -> tuple[MonitoringSession | None, MonitoringNote | None]:
    """Create a ``MonitoringSession`` (only if ``duration_seconds`` is
    given) and, only if ``note_text`` is non-blank, a ``MonitoringNote``
    linked to it (with get-or-created tags). If no duration is given but a
    note is, the note is created standalone (``session=None``). If
    neither is given, raises ``ValidationError``. Atomic: if any part
    fails, nothing is persisted.

    ``pregnancy`` is resolved once, from ``patient.current_pregnancy`` --
    None when she has no active pregnancy yet (or between two), in which
    case the session/note is still created, just not tied to an episode.

    ``left_voicemail``/``two_way_communication`` are call-outcome flags on
    the note -- the caller (``CombinedMonitoringSerializer``) has already
    validated they're only set alongside a non-blank ``note_text`` and that
    at most one is true.
    """
    cleaned_note_text = (note_text or "").strip()
    if not duration_seconds and not cleaned_note_text:
        raise serializers.ValidationError("Provide at least a duration or a note.")

    pregnancy = patient.current_pregnancy

    session = None
    if duration_seconds:
        session = MonitoringSession.objects.create(
            patient=patient,
            pregnancy=pregnancy,
            duration_seconds=duration_seconds,
            recorded_at=recorded_at,
            added_by=added_by,
        )

    if not cleaned_note_text:
        return session, None

    note = MonitoringNote.objects.create(
        patient=patient,
        pregnancy=pregnancy,
        session=session,
        note=cleaned_note_text,
        recorded_at=recorded_at,
        added_by=added_by,
        left_voicemail=left_voicemail,
        two_way_communication=two_way_communication,
    )
    if tags:
        note.tags.set(get_or_create_tags(tags, location=patient.location))
    return session, note


def format_duration(total_seconds: int) -> str:
    """Abbreviated human-readable duration: "26m 3s", "1h 15m 8s", "7d 4h 45m 12s"."""
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {minutes}m {seconds}s"
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s"


def month_bounds(*, year: int, month: int, tzinfo):
    """Start and end-of-day of the given calendar month, in ``tzinfo``.

    Anchors off ``timezone.now()`` converted to the target zone, then
    replaces date components -- avoids constructing a datetime directly in
    a DST-observing zone, which can silently misinterpret the offset. Same
    approach as Neuro_RPM's own ``month_bounds``.
    """
    anchor = timezone.localtime(timezone.now(), timezone=tzinfo)
    start = anchor.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
    last_day = calendar.monthrange(year, month)[1]
    end = anchor.replace(year=year, month=month, day=last_day, hour=23, minute=59, second=59, microsecond=999999)
    return start, end


def monitoring_period_totals(*, start, end, **filters) -> dict:
    """Total monitoring seconds logged within [``start``, ``end``] (inclusive)
    on ``recorded_at``, additionally filtered by ``**filters`` -- e.g.
    ``patient=patient`` for a patient's own totals, or ``added_by=staff.user``
    for a staff member's activity totals (see ``staff.services.
    compute_audit_report``). Shared aggregation core for both calendar-month
    totals (below) and any rolling-window caller -- the query shape is
    defined once here regardless of which column narrows it.

    Unlike Neuro_RPM's version, there is no per-program split to report --
    MomCare has exactly one programme, so this is a single total, not an
    rpm/ccm/oor breakdown.
    """
    total = (
        MonitoringSession.objects.filter(
            recorded_at__gte=start,
            recorded_at__lte=end,
            **filters,
        ).aggregate(total=Sum("duration_seconds"))["total"]
        or 0
    )
    return {"total_seconds": total, "total_formatted": format_duration(total)}


def monitoring_month_totals(*, patient, year: int, month: int) -> dict:
    """Total monitoring seconds logged for ``patient`` in the given
    calendar month, computed in the patient's location timezone off
    ``recorded_at`` (not ``created_at``).
    """
    start, end = month_bounds(year=year, month=month, tzinfo=patient.location.timezone)
    return monitoring_period_totals(patient=patient, start=start, end=end)
