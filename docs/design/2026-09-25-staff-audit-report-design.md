# Staff Audit Report — Design

**Date:** 2026-09-25
**Status:** Approved for implementation.

## Context

Inspired by a competitor platform (TOCA Health)'s "Staff Audit Report" — a per-staff activity
dashboard (caseload, monitoring time, call outcomes, out-of-range handling), scoped by location,
with self-only visibility for regular staff and full visibility for admins/location managers.

TOCA's version is split across two billing programmes (RPM/CCM) it bills Medicare for. MomCare
runs exactly one programme and bills no insurer, so that split does not exist here and is **not**
built — no "RPM Patients"/"CCM Patients"/"Compliance %" fields, since none of those concepts have
a MomCare equivalent and faking them would mean permanently-zero or meaningless numbers. This is
a single-programme report, built from data MomCare already tracks. No new models — this is a
read-only aggregation endpoint over `Staff`, `MonitoringSession`, `MonitoringNote`, and `Alert`.

## KPIs (what the API returns) and where each comes from

| KPI | Source | Notes |
|---|---|---|
| `total_patients` | `Staff.current_patient_count` | Already a computed property — no new query. |
| `monitoring_time.total_seconds` / `total_formatted` | `MonitoringSession.duration_seconds`, filtered `added_by=staff.user` | Generalizes the existing `monitoring_period_totals()` (currently filters by `patient=`) to accept either filter — see "Implementation notes" below. |
| `monitoring_time.distribution` | Same table, grouped by `recorded_at__date` | Powers a day-by-day chart, same idea as TOCA's "Monitoring Time Distribution." |
| `call_outcomes.two_way_count` / `voicemail_count` | `MonitoringNote.two_way_communication` / `left_voicemail`, filtered `added_by=staff.user` | Both already exist as booleans on every note. |
| `call_outcomes.call_success_rate` | `two_way_count / (two_way_count + voicemail_count)` | 0 when there are no calls in the period, not a division error. |
| `alerts_handled.acknowledged_count` | `Alert.objects.filter(acknowledged_by=staff.user, acknowledged_at__range=...)` | |
| `alerts_handled.resolved_count` | `Alert.objects.filter(resolved_by=staff.user, resolved_at__range=...)` | |

## API

```
GET /api/staff/{staff_id}/audit-report/?period=<code>
```

`period` is a preset rolling window ending now, not a specific calendar month to browse — a
dropdown of fixed lookback ranges, not a month-by-month picker (that's the deliberate difference
from `MonitoringView.resolve_month_range()`, which this endpoint does **not** reuse).

| `period` code | Window |
|---|---|
| `2d` | Last 2 days |
| `week` | Last 7 days |
| `month` (default) | Last 30 days |
| `3month` | Last 90 days |
| `6month` | Last 180 days |
| `year` | Last 365 days |
| `2year` | Last 730 days |

Any other value is a **400** (`{"period": ["Must be one of: 2d, week, month, 3month, 6month, year, 2year."]}`),
not a silent fallback — a typo'd filter should never quietly return the wrong window. `start`/`end`
are computed as `[now - N days, now]` in UTC, not a per-location timezone — unlike `Patient`, a
`Staff` row has no single canonical location (locations live on `User`, many-to-many), and a
rolling N-day window has no calendar-day boundary to get right the way month-browsing does, so
there is nothing a per-location timezone would actually change here.

Response:

```json
{
  "staff": {"id": "...", "name": "Dr. Sana Malik", "role": "provider", "employee_id": "EMP-001"},
  "period": {"code": "month", "start": "2026-08-26T00:00:00+05:00", "end": "2026-09-25T14:00:00+05:00"},
  "total_patients": 6,
  "monitoring_time": {
    "total_seconds": 3600,
    "total_formatted": "1h 0m 0s",
    "distribution": [{"date": "2026-09-01", "seconds": 600}]
  },
  "call_outcomes": {
    "two_way_count": 4,
    "voicemail_count": 2,
    "call_success_rate": 0.67
  },
  "alerts_handled": {
    "acknowledged_count": 3,
    "resolved_count": 2
  }
}
```

## Access control

Reuses `staff/services.py::can_manage_staff(requester, staff)` directly — it already implements
exactly this rule (hospital_admin manages everyone at their hospital; a location manager manages
staff assigned to at least one location they manage) for onboarding/deactivate/reactivate/edit.
No new permission logic:

- `is_self = member.user_id == request.user.id`
- `can_manage = can_manage_staff(request.user, member)`
- Allowed if `is_self or can_manage`; otherwise **403** (existence is not a secret within one
  hospital — the staff directory already lists everyone).
- A `staff_id` belonging to another hospital never reaches this check at all — `StaffScopedView.
  get_staff_or_404` scopes by `organization_lookup = "user__organization"` first, so it resolves
  to **404** before any permission logic runs (existence not confirmed across tenants — the
  standing rule everywhere else in this codebase).

## Implementation notes

- New `core/staff/services.py::AUDIT_PERIODS = {"2d": 2, "week": 7, "month": 30, "3month": 90,
  "6month": 180, "year": 365, "2year": 730}` and `resolve_audit_period(code)` — validates `code`
  against that dict (raises `serializers.ValidationError` on an unknown one, caught by the view as
  the 400 above) and returns `(start, end)` as `(now - timedelta(days=N), now)` in UTC. Deliberately
  separate from `month_bounds()`/`resolve_month_range()` in `monitoring/services.py` — those answer
  "which specific calendar month," this answers "how far back from right now," and conflating the
  two would make both harder to read.
- `core/monitoring/services.py::monitoring_period_totals(*, start, end, **filters)` — generalize
  its single `patient=` keyword into `**filters` passed straight to `.filter()`, so the existing
  patient-scoped call site (`monitoring_period_totals(patient=patient, ...)`) and the new
  staff-scoped one (`monitoring_period_totals(added_by=staff.user, ...)`) share one aggregation
  instead of two near-identical copies. Works unchanged for either a calendar month's `start`/`end`
  or a `resolve_audit_period()` rolling window — it only ever consumed a start/end pair.
- New view: `StaffAuditReportView` in `core/staff/api/views.py`, `organization_lookup =
  "user__organization"` (matches the existing `Staff → user__organization` scoping path already
  documented in CLAUDE.md).
- New service function `core/staff/services.py::compute_audit_report(staff, start, end)` —
  assembles the dict above from the three existing apps' queries. Kept in `staff/services.py`
  rather than `monitoring/services.py` since it also reads `Alert` (a different app) and the
  report itself is fundamentally about a `Staff` row, not a monitoring record.
- `monitoring_time.distribution` stays day-granularity at every window length, including `2year`
  (730 rows worst case) — small enough to return as-is. No week/month bucketing for long windows;
  if a real hospital's chart gets visually noisy at that length, the frontend can downsample for
  display. Not adding bucketing logic nobody has asked for yet.

## Out of scope

- No RPM/CCM fields, no "compliance %" — no MomCare concept backs either.
- No `onboarded_by`/"patients onboarded this month" — would require a new field on `Patient`
  that doesn't exist today; that's a schema change, not a reporting one, and nothing has asked
  for it yet.
- No caching/materialization — this aggregates a handful of rows per staff per period; add it
  later only if a real hospital's data volume makes it necessary.
- No custom/arbitrary `start`/`end` query params — only the seven preset codes above. A free-form
  date-range picker is a different, larger feature; add it later if the preset list turns out not
  to be enough.
