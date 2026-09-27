"""Small, framework-light display helpers shared across serializers."""

from __future__ import annotations

from datetime import datetime

from django.utils import timezone


def humanize_days_ago(instant: datetime | None, tzinfo, *, now: datetime | None = None) -> str | None:
    """ "Today" / "Yesterday" / "N days ago", comparing ``instant`` to now in
    ``tzinfo`` -- ported verbatim from Neuro_RPM's own ``humanize_days_ago``
    (``core/common/formatting.py`` in that codebase). ``None`` in, ``None``
    out, so a patient with no reading/contact yet stays visibly "never",
    not a fabricated "Today".

    ``now`` defaults to ``timezone.now()``; accepting it explicitly lets
    tests pin an exact day-of-month/timezone boundary without mocking the
    clock, the same convention this project already uses for the Care
    Activity condition builders.
    """
    if instant is None:
        return None

    now = now or timezone.now()
    today = timezone.localtime(now, timezone=tzinfo).date()
    instant_date = timezone.localtime(instant, timezone=tzinfo).date()

    days = (today - instant_date).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    return f"{days} days ago"
