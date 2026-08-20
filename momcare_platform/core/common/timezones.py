"""Single home for timezone math.

Storage is UTC everywhere (``USE_TZ=True``). Zone intent is always an IANA name
(never a persisted offset). Display uses the logged-in user's zone; month-based
business rules use the patient's Location zone.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

UTC = ZoneInfo("UTC")


def _as_zoneinfo(tz) -> ZoneInfo:
    return tz if isinstance(tz, ZoneInfo) else ZoneInfo(str(tz))


def resolve_display_tz(user) -> ZoneInfo:
    """Display zone for ``user`` — their timezone, or UTC when absent/anonymous."""
    tz = getattr(user, "timezone", None)
    return _as_zoneinfo(tz) if tz else UTC


def month_start_utc_for_tz(tz, instant: datetime | None = None) -> datetime:
    """Aware UTC instant of 00:00 on the first of ``instant``'s month, seen from ``tz``."""
    tz = _as_zoneinfo(tz)
    instant = instant or timezone.now()
    local = instant.astimezone(tz)
    local_month_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return local_month_start.astimezone(UTC)


def resolve_location_tz(location) -> ZoneInfo:
    """Effective IANA zone for `location`: its own, else its organization's, else UTC."""
    tz = getattr(location, "timezone", None)
    if not tz:
        org = getattr(location, "organization", None)
        tz = getattr(org, "timezone", None) or UTC
    return _as_zoneinfo(tz)


def location_month_start(location, instant: datetime | None = None) -> date:
    """First day of ``instant``'s month as seen from ``location``'s timezone.

    Falls back to the organization's timezone, then UTC.
    """
    tz = resolve_location_tz(location)
    instant = instant or timezone.now()
    return instant.astimezone(tz).date().replace(day=1)


def day_bounds_utc_for_tz(tz, day: date) -> tuple[datetime, datetime]:
    """(start_utc, end_utc_exclusive) for calendar ``day`` as seen from ``tz``."""
    tz = _as_zoneinfo(tz)
    start_local = datetime(day.year, day.month, day.day, tzinfo=tz)
    return start_local.astimezone(UTC), (start_local + timedelta(days=1)).astimezone(UTC)
