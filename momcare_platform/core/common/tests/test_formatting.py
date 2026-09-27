"""``humanize_days_ago`` -- ported verbatim from Neuro_RPM's own function of
the same name, so the display convention matches exactly.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from momcare_platform.core.common.formatting import humanize_days_ago

UTC = ZoneInfo("UTC")
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def test_none_in_none_out():
    assert humanize_days_ago(None, UTC, now=NOW) is None


def test_today_is_today():
    instant = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)
    assert humanize_days_ago(instant, UTC, now=NOW) == "Today"


def test_yesterday():
    instant = datetime(2026, 9, 26, 23, 0, tzinfo=UTC)
    assert humanize_days_ago(instant, UTC, now=NOW) == "Yesterday"


def test_several_days_ago():
    instant = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    assert humanize_days_ago(instant, UTC, now=NOW) == "6 days ago"


def test_a_future_instant_reads_as_today_not_negative_days():
    """Clamped, not negative -- a clock-skew edge case should never surface
    as "-1 days ago"."""
    instant = datetime(2026, 9, 28, 3, 0, tzinfo=UTC)
    assert humanize_days_ago(instant, UTC, now=NOW) == "Today"


def test_respects_the_given_timezone_at_a_day_boundary():
    """A moment that's "yesterday" by UTC's calendar can still be "today"
    in a timezone several hours ahead -- the whole reason this takes a
    tzinfo rather than always comparing in UTC."""
    karachi = ZoneInfo("Asia/Karachi")  # UTC+5
    now_karachi = datetime(2026, 9, 27, 2, 0, tzinfo=karachi)  # 27th, 2 AM Karachi
    # 11:30 PM UTC on the 26th = 4:30 AM Karachi on the 27th -- same
    # calendar day as "now" in Karachi, even though it's the 26th in UTC.
    instant_utc = datetime(2026, 9, 26, 23, 30, tzinfo=UTC)

    assert humanize_days_ago(instant_utc, karachi, now=now_karachi) == "Today"
