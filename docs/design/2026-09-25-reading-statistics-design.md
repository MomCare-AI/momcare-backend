# Reading Statistics & Vitals Summary — Design

**Date:** 2026-09-25
**Status:** Approved for implementation.

## Context

Inspired by Neuro_RPM's two independently-built features on `PatientReadingViewSet` and
`PatientViewSet` — reading statistics (average/min/max/category-percentage over a filterable
date window) and Vitals Summary (a fixed rolling-30-day average across every vital). Neither
is AI/ML-driven on the reference side — both are plain Django `Avg()`/`Count()` aggregation
(`services.py:1740-1816`, `1898-1925`); MomCare's own trained model (`momcare_model/`) is a
separate feature entirely (risk scoring), untouched by this work.

Neuro_RPM's implementation is more complex than MomCare needs, for a reason specific to their
schema: their readings are split across several per-vital-type tables with generic
`value_1..value_5` columns, so heart rate (recorded in two different tables) needs a manual
weighted-average merge, and classification needs a runtime field-name remapping. **MomCare's
`VitalReading` is one flat row with named columns** (`systolic_bp`, `heart_rate`, `body_temp_f`,
`blood_glucose`, `hemoglobin`, plus `age`/`stress_score`/`phys_activity_score`, which have no
defined clinical category and are excluded from the `categories` block) — none of that
complexity exists here. Both features reduce to a single DB aggregate query each.

MomCare also already has the module Neuro_RPM's `guideline_bands.py` plays:
`momcare_model/clinical_categories.py` — a pure function library (`bp_category(systolic,
diastolic)`, `heart_rate_category(hr)`, `temperature_category(temp)`, `glucose_category(g)`,
`hemoglobin_category(hb)`), independent of `RiskAssessment`. Category percentages classify raw
readings directly through these existing functions — they do **not** read `RiskAssessment`,
whose rows are sparse (written only on risk-level transitions, per `reassess_risk()`'s own
docstring), not one per reading.

**Decision, made with the user**: Neuro_RPM's "previous period comparison" (average vs. the
immediately-preceding equal-length window, only for windows >30 days) is **not** built. It's
the most complex piece of their implementation and the least obviously useful; nothing has
asked for it, and a trend chart already answers "how is this changing" better than one delta
number. Revisit only if specifically requested later.

## Feature 1: Reading Statistics — extends the existing reading list

`GET /api/pregnancies/{pregnancy_id}/readings/` already exists (`ReadingListCreateView`,
currently only `?since=`). Extending it, matching Neuro_RPM's own choice to embed statistics
in the list endpoint rather than a separate one:

**New query params**:
- `period` — one of `2_days | 1_week | 1_month | 3_months | 6_months | 1_year` (Neuro_RPM's
  exact enum). An unrecognized value is a 400. Resolved as day-boundary UTC bounds in the
  pregnancy's location timezone (`pregnancy.patient.location.timezone`), same pattern as
  `monitoring/services.py::month_bounds()` — inclusive of "today" regardless of time of day.
- `start_date` / `end_date` (`YYYY-MM-DD`) — custom range, takes precedence over `period` if
  both given. `start_date > end_date` is a 400.
- No date param at all → full history, unchanged from today's behavior (`since` stays as
  an alias/lower-bound-only option for backward compatibility with existing callers).
- `reading_type` — **this is what triggers statistics, no separate flag.** One of
  `blood_pressure | temperature | blood_glucose | hemoglobin | wellness`, scoping the
  `statistics` block to only that group's fields:

  | `reading_type` | Fields |
  |---|---|
  | `blood_pressure` | `systolic_bp`, `diastolic_bp`, `heart_rate` |
  | `temperature` | `body_temp_f` |
  | `blood_glucose` | `blood_glucose` |
  | `hemoglobin` | `hemoglobin` |
  | `wellness` | `stress_score`, `phys_activity_score` (no `categories` — no clinical bands are defined for either) |

  Matches Neuro_RPM's own trigger exactly: no `reading_type` → `statistics` is `{}`, same as
  their endpoint when `reading_type` is omitted. Heart rate is bundled under `blood_pressure`
  only (no standalone `heart_rate` type) — mirrors Neuro_RPM's own example response, where
  asking for `blood_pressure` statistics returns systolic/diastolic/heart_rate together, not
  isolated. An unrecognized `reading_type` is a 400.

**`statistics` shape**, e.g. `?reading_type=blood_pressure`:

```json
{
  "statistics": {
    "average": {"systolic_bp": 144, "diastolic_bp": 96, "heart_rate": 91},
    "min": {"systolic_bp": 138, "diastolic_bp": 92, "heart_rate": 78},
    "max": {"systolic_bp": 150, "diastolic_bp": 100, "heart_rate": 110},
    "readings_count": {"systolic_bp": 3, "diastolic_bp": 3, "heart_rate": 3},
    "categories": {
      "bp_category": {
        "guideline": "ACC/AHA Blood Pressure Guidelines",
        "bands": [
          {"key": "Hypotensive", "count": 0, "percentage": 0},
          {"key": "Normal", "count": 0, "percentage": 0},
          {"key": "Elevated", "count": 0, "percentage": 0},
          {"key": "Stage 1", "count": 0, "percentage": 0},
          {"key": "Stage 2", "count": 3, "percentage": 100},
          {"key": "Hypertensive Crisis", "count": 0, "percentage": 0}
        ]
      },
      "heart_rate_category": {
        "guideline": "American Heart Association Heart Rate Guidelines",
        "bands": [
          {"key": "Bradycardia", "count": 0, "percentage": 0},
          {"key": "Normal", "count": 2, "percentage": 67},
          {"key": "Tachycardia", "count": 1, "percentage": 33}
        ]
      }
    }
  }
}
```

Only the fields belonging to the requested `reading_type` appear — a `?reading_type=temperature`
call returns only `body_temp_f` in `average`/`min`/`max`/`readings_count`, and only
`temperature_category` in `categories`. Empty window (no readings, or no qualifying data for
that type) → every block is an empty dict, never omitted keys or a divide-by-zero.

## Implementation notes (Feature 1)

- `modules/pregnancy/vitals/services.py::READING_PERIODS` + `resolve_reading_period(code,
  tzinfo)` — same shape as `staff/services.py::AUDIT_PERIODS`/`resolve_audit_period()` from
  yesterday's work, but day-boundary-aligned (not a rolling now-minus-N-days window) since these
  are calendar-day presets a clinician picks ("this month"), not a "how far back" filter.
- `READING_TYPE_FIELDS = {"blood_pressure": [...], "temperature": [...], ...}` — the table
  above, one dict, the single source for which raw fields and which category function(s) a
  given `reading_type` maps to. An unrecognized `reading_type` raises the same
  `serializers.ValidationError`-caught-as-400 pattern as `resolve_audit_period()`.
- `compute_reading_statistics(queryset, reading_type)` — **real DB aggregation**, one
  `.aggregate(...)` call building `Avg`/`Min`/`Max`/`Count` annotations for **only that type's
  fields** (2-3 fields, not all 9) — not Neuro_RPM's Python-list approach, which existed only to
  solve their multi-table field-remapping problem.
- `allocate_percentages(counts, total)` — **ported verbatim from Neuro_RPM's
  `guideline_bands.py:173-184`** into `momcare_model/` (framework-free, matches the
  `momcare_model stays framework-free` import-linter contract already in place) — largest-
  remainder rounding, guarantees percentages sum to exactly 100, already proven correct by
  Neuro_RPM's own tests (thirds case, 67/33 case).
- `round_metric_value(metric, value)` + `METRIC_ROUNDING` (same module, `momcare_model/
  statistics.py`) — display rounding for `average`/`min`/`max`, **per metric, not a uniform 2
  decimals** (the first implementation pass's mistake, corrected 25 Sep 2026 after the user
  asked whether this matched Neuro_RPM's own convention — it didn't yet). Ported from
  Neuro_RPM's own `METRIC_ROUNDING`: `systolic_bp`/`diastolic_bp`/`heart_rate` → 0 decimals,
  returned as `int` (`125.9` → `126` — nearest whole number, **not** a nearest-ten bucket);
  `body_temp_f`/`blood_glucose`/`hemoglobin` → 1 decimal, returned as `float`. Extended for the
  two vitals Neuro_RPM doesn't have — `stress_score`/`phys_activity_score` get 1 decimal,
  same "continuous measurement" tier as glucose/temperature. An unrecognized metric defaults to
  1 decimal, matching Neuro_RPM's own fallback. Applied identically in `compute_vitals_summary`
  (Feature 2) for the same reason: one display convention, not a per-endpoint one.
- Category tally: only for `reading_type`s that map to a classifiable field (not `wellness`) —
  run every reading's raw value(s) through the matching `clinical_categories.py` function,
  `Counter` the non-blank results (blank `""` = vital missing on that reading, excluded from
  that vital's tally and count), then `allocate_percentages`. `bp_category` needs both
  `systolic_bp` and `diastolic_bp` from the same row, matching `clinical_categories.
  bp_category()`'s own two-argument signature — `blood_pressure` is the only `reading_type`
  producing two `categories` entries (`bp_category` and `heart_rate_category`) since it bundles
  two classifiable fields; every other type produces at most one. A category with zero valid
  classifications (no reading in the window carried that vital at all) is **omitted from
  `categories` entirely** — not shown with all-zero bands — matching Neuro_RPM's own
  `_compute_categories` behavior (this needed a fix after the first implementation pass showed
  0%-everywhere bands instead of an empty dict on a truly empty window, caught by the test for
  that exact case).

## Feature 2: Vitals Summary — new endpoint

`GET /api/pregnancies/{pregnancy_id}/vitals-summary/` — fixed rolling `[now-30d, now)` window,
no query params. One `.aggregate(Avg(...))` call across `systolic_bp`, `diastolic_bp`,
`heart_rate`, `body_temp_f`, `blood_glucose`, `hemoglobin`. `age`/`stress_score`/
`phys_activity_score` excluded (age isn't a trend metric; stress/activity scores have no
established clinical unit to summarize this way — nothing has asked for them here).

```json
{
  "last_30_days_average": {
    "systolic_bp": 125,
    "diastolic_bp": 85,
    "heart_rate": 75,
    "body_temp_f": null,
    "blood_glucose": null,
    "hemoglobin": null
  }
}
```

`null`, not `0` or an omitted key, for a vital with zero readings in the window — matches
Neuro_RPM's own explicit test assertion and MomCare's own "never show a fabricated normal-
looking value" rule already stated on `LatestReadingsView`.

## Access control

Both reuse the existing `MonitoringView` base (`modules/pregnancy/vitals/api/views.py`) —
`IsHospitalStaff`, `organization_lookup = "patient__location__organization"`,
`get_pregnancy_or_404`. No new permission logic; same as every other reading endpoint.

## Out of scope

- No previous-period comparison (see Decision above).
- No AI/ML involvement — both features are arithmetic over stored readings.
- No caching — these aggregate at most a few hundred rows per pregnancy per window.
