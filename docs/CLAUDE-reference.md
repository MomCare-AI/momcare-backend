# CLAUDE.md

This file provides guidance to Claude Code (or any AI agent) when working on this repository.

## Overview

`momcare_platform` is a **backend-only, API-only** Django suite for maternal-health B2B SaaS.
Hospitals register on the platform, then onboard their own staff and patients. There is no
server-rendered UI — the only HTML surfaces are the Django admin and the Swagger docs. The
frontend is a separate SPA that talks to `/api/`.

This project follows the **Universal Modular Monolith Blueprint** — see that document (from
the sibling reference project, Neuro_RPM) for the full architectural reasoning. This file
captures the decisions specific to MomCare.

## Tenancy — read this before touching any model

MomCare is **shared-schema multi-tenant**, not single-tenant like the Neuro_RPM reference
project. Many hospitals share one database. `Organization` is a real, multi-row table — one
row per hospital — not a singleton. Every tenant-owned model carries an `organization`
column (directly, or via `Location.organization`), enforced two ways.

`Patient` carries **both**: it reaches its hospital through `location__organization` (which
is still the scoping path every queryset uses — see "Scoping paths" below), and it also has
a direct `organization` FK, denormalized the same way `Device` has one. That column exists so
the per-hospital CNIC uniqueness constraint can be scoped correctly — one hospital can have
several locations, so a location-scoped constraint would miss a duplicate CNIC at a different
branch of the same hospital. Do not "simplify" it away.

Enforcement:

1. **Application-level**: the scoping mixins in `core/common/scoping.py`
   (`OrganizationScopedQuerysetMixin`, `LocationScopedQuerysetMixin`) — compose one of these
   into every viewset over tenant-owned data.
2. **Database-level**: Postgres Row-Level Security policies — implemented, covering every
   tenant-owned table, fail-closed by design (an unset session variable sees zero rows, not
   every hospital's rows — see the migration's own docstring for why that took a second pass
   to get right). Verified against a real non-bypassing role, including a subtle bug where
   SimpleJWT resolved identity *before* RLS's per-request scoping ran, locking every hospital
   user out the moment enforcement was real (fixed — see git log for
   `momcare_platform/core/organization/migrations/0006_row_level_security.py` and
   `core/users/api/auth.py`'s `TenantAwareJWTAuthentication`).

   **As of 1 Sep 2026, this is actually enforced in production**, not just written and
   tested. `DATABASE_URL` (what `web`/gunicorn connects as) now points at a dedicated,
   restricted `momcare_app` role — `LOGIN`, `NOSUPERUSER`, `NOBYPASSRLS`, grants limited to
   `SELECT/INSERT/UPDATE/DELETE` on tables and `USAGE/SELECT` on sequences, no
   `DROP`/`ALTER`/`TRUNCATE`. DDL (`migrate`, `createcachetable`) still needs the
   table-owning role, so it runs against a second connection string,
   `MIGRATION_DATABASE_URL` — the switch between the two is done by command-name detection
   in `config/settings/production.py` (`sys.argv[1] in {"migrate", "createcachetable"}`),
   deliberately not in the `Procfile`. See `DEPLOY.md`'s "Database roles" section for the
   full rollout order and the real incident (2 Sep 2026) caused by this variable going
   missing on Railway. Locally, `local.py`/`test.py` still use one ordinary role with
   `BYPASSRLS`, so RLS is not exercised by a normal local run — use
   `scripts/verify_rls.py` (see its own docstring for why it isn't a pytest test) to
   verify the policies directly against a non-bypassing role.

A missed scoping check on tenant data is a cross-hospital PHI leak, not a bug ticket. Treat
"did I compose the scoping mixin" as a mandatory review item on every new viewset.

## Roles — two-tier

- **Platform tier** (`platform_admin`): MomCare's own operators. Not scoped to any single
  hospital — `User.organization` is null for this role. Bypasses tenant scoping entirely.
- **Hospital tier** (`hospital_admin`, `provider`, `nurse`, `care_manager`, `patient`):
  belongs to exactly one hospital via `User.organization`.

Role codes are constants on `settings` (`ROLE_PLATFORM_ADMIN`, `ROLE_HOSPITAL_ADMIN`, etc.),
matched against `User.role.code` — never hardcode role strings.

## Commands

Everything runs through `uv`. Default settings module is `config.settings.local`.

```bash
uv sync                                   # install deps
uv run python manage.py migrate           # apply migrations
uv run python manage.py createsuperuser   # bootstraps a platform_admin (see users/managers.py)
uv run python manage.py runserver         # dev server

uv run pytest                             # full suite (runs on Postgres — see settings/test.py)
uv run pytest --create-db                 # force a fresh test DB

uv run ruff check .                       # lint
uv run ruff format .                      # format
uv run mypy momcare_platform               # type check
uv run lint-imports                       # import-linter boundary contracts
uv run pre-commit run --all-files
```

URLs: `/admin/` (admin), `/api/` (REST), `/api/docs/` (Swagger, admin login required),
`/health/` (health check, also served at `/`).

## Architecture

### Layout: `core/` vs `modules/`
- `momcare_platform/core/` — foundational apps: `common` (no models of its own — shared base
  classes, permissions, scoping, gating, middleware), `users`, `organization`, `locations`,
  `staff`, `patients`. Also `platform_admin` (added 2026-09-13) — **an empty skeleton on
  purpose**, deliberately deferred (`api/views.py`/`api/serializers.py` are
  `"""TODO: implement."""` stubs, no routes mounted). It will operate on
  `Organization`/`OrganizationDeactivationRequest` and hold the project's only deliberately
  cross-tenant API surface once built. Until then, reviewing hospital applications and
  deactivation requests happens entirely through Django admin (`OrganizationAdmin`,
  `OrganizationDeactivationRequestAdmin`) — do not build the `platform_admin` API without
  being asked; the app exists as a placeholder, the same way `momcare_platform/modules/`
  does below.
- `momcare_platform/modules/` — feature programs. **Currently empty.** The first module
  (maternal-health monitoring) has not been designed yet — that is deliberately a separate
  design pass from this foundation, per the blueprint's scope-decomposition guidance.

Dependency direction is strictly **`modules → core`**, enforced by `import-linter`
(`pyproject.toml [tool.importlinter]`). `core` may not import `modules`; `config.api_router`
may not hard-import `modules`.

### Program registry
Same self-registration pattern as the blueprint: a feature module registers a `ProgramSpec`
from its own `AppConfig.ready()` via `core/common/programs.py`. `config/api_router.py` mounts
routes for every registered program without ever naming one. **Unlike Neuro_RPM**, there is
no clinical `ProgramCode` enum in `programs.py` here — that's medical-domain content that
belongs to the first feature module's own design.

### Module activation is per-hospital, not global
`ModuleRegistry` carries an `organization` FK — each hospital independently activates the
feature modules it's subscribed to. This is a deliberate divergence from Neuro_RPM (whose
single-tenant `ModuleRegistry` has no organization concept) — see `core/organization/models.py`
and `core/common/gating.py`.

### Shared model bases (`core/common/models.py`)
- `UUIDPrimaryKeyModel` — all first-party models use UUID PKs.
- `TimeStampedModel` — `created_at`/`updated_at`.
- `AddressMixin` — structured address columns.
- `Deactivatable` — soft-deactivation; **records are never physically deleted**.

## Status of this codebase

**Superseded, 24 Aug 2026.** This section previously described a freshly scaffolded
foundation with stub services and views, and told you not to add business logic. That
was accurate on 15 August and is now wrong in every particular — it was left in place
long enough to contradict the rest of this file.

The platform is built and running. Eight capabilities are complete, tested and pushed:

| # | Capability |
|---|---|
| 1 | Hospital registration with a review gate — pending / approved / rejected / suspended, evidence recorded |
| 2 | Six roles, enforced by DRF permission classes |
| 3 | Staff lifecycle — hospital_admin or a location's own manager **invites** the account (created passwordless with `requires_password_reset=True`; a one-time set-password link is emailed, reusing the reset-token flow). **No password is ever accepted from an admin**, at onboarding or update — only the staff member knows it, which is what makes alert acknowledgement provable. `has_activated` on the staff row and `requires_password_reset` on `/auth/me/` expose the pending state. Then updates role/locations, deactivates/reactivates, and hard-deletes once deactivated and free of protected clinical history. An optional `phone` is collected as contact information — see "Sign-in is email only" below for the blank-to-NULL trap it carries |
| 4 | Patients and pregnancy — consent history, obstetric dating, risk factors |
| 5 | Vitals and devices — ingestion, charts. The old fake-data simulator (`SimulateReadingsView`) was removed entirely — see `MEMORY.md` §7 |
| 6 | Risk assessment — a real trained model (`momcare_model/`, XGBoost, 88.4% test accuracy), not a rules engine — see "The clinical modules" below |
| 7 | Alerts and escalation — three-tier ladder, in-portal notifications only (no email leg — removed deliberately, see below), append-only audit trail |
| 8 | Clinical contact logging — sessions, notes, and a tenant-scoped tag catalogue (`core/monitoring`, `app_label="clinical_notes"`) — see "Apps and what each owns" below |
| 9 | Care Activities — live recency/threshold roster signals distinct from the review-status workflows above, backed by a new `core/analytics` app. Three built: Monitoring Follow-up, Unseen Readings, Reading Reminder — see "Care Activities" below |

**878 backend tests (4 skipped, whole suite — `uv run pytest`) as of the 27 Sep 2026 revision,
30 frontend.** `momcare_platform/core` alone is 715 of those — see the `Testing` section below
for that narrower, faster command.
Security-critical tests validated by fault injection: each protection was
deliberately removed and the corresponding test confirmed to fail.

`services.py` and `api/` are real implementations throughout, not stubs. Business
logic exists and is expected to.

### Devices, readings, risk and alerts live under `modules/pregnancy/`, not `core/`

**Moved 16 Sep 2026.** Until this date the clinical work lived entirely under `core/` —
`core/monitoring` (`Device`, `VitalReading`, `RiskAssessment`) and `core/alerts` (`Alert`,
`AlertEvent`). That was always a mismatch with the reference platform: in Neuro_RPM,
`core/monitoring` means clinical **notes** (`ClinicalTag`, `MonitoringSession`,
`MonitoringNote`), while devices, readings and alerts are **module** content
(`modules/rpm/devices`, `modules/rpm/vitals`, `modules/rpm/alerts`). MomCare had put their
module content in a folder wearing their core app's name. The user is building
`MonitoringSession`/`MonitoringNote` next (see "What genuinely does not exist yet") and
those need the `core/monitoring` name — so this moved to free it.

Current layout:

```
core/monitoring/               <- does not exist. Recreate it fresh for MonitoringSession
                                   /MonitoringNote/ClinicalTag when that work starts.
modules/pregnancy/
    vitals/     Device, VitalReading, RiskAssessment    (one app, not split — see below)
    alerts/     Alert, AlertEvent
```

`modules/pregnancy/`, not `rpm/` — RPM is the reference platform's product; this one is
maternal health. **`vitals` deliberately stays one app holding all three models**,
not split into separate `devices/` + `vitals/` as first proposed — Neuro_RPM's exact
split needs two different `app_label`s for models that currently share one, which turns a
zero-risk folder move into a real schema migration for no benefit the user asked for. If
`devices` ever earns its own app, that's a second, later move.

#### Why this was safe: app labels didn't change, table names didn't change

Django's `app_label` — not the Python package path — is what migration history and table
names key off. Both moved apps kept their **original label** (`alerts/apps.py`:
`label = "alerts"`; `vitals/apps.py`: `label = "monitoring"`, deliberately mismatched from
its new folder name, same kind of artifact Neuro_RPM itself accepts for `CarePlan`'s
`rpm_care_plans` label). Consequence: `alerts_alert`, `alerts_alertevent`,
`monitoring_device`, `monitoring_vitalreading`, `monitoring_riskassessment` are still the
literal table names, `manage.py makemigrations --check` finds **zero new migrations**, and
the RLS policies (which reference table names, not labels) needed no changes at all —
verified with `scripts/verify_rls.py` post-move. If a table is ever renamed to fully match
its new label, RLS policies must be rewritten in the same migration; that didn't happen here.

#### What had to change: two import-linter contracts, and two `core` call sites

- **`core must not import modules` — unaffected, still enforced.** `core/patients` and
  `core/organization` (`demo_setup.py`) both read `RiskAssessment`/`VitalReading`/`Alert`
  directly; those four call sites now resolve them via
  `django.apps.apps.get_model("monitoring", "RiskAssessment")` (a runtime lookup, not a
  static import, so the contract's AST-based check never sees it) instead of a top-level
  `from ... import`. One call site needed `reassess_risk` itself, a function rather than a
  model — `importlib.import_module(...).reassess_risk`, same reasoning. This is the exact
  pattern Neuro_RPM's own code uses for `core.patients` reading `CarePlan` out of
  `modules.rpm.care_plans` — read their comment on that lookup before touching this again.
- **`project wiring must not hard-import modules` — removed.** It existed to force module
  routes through the `ProgramSpec` registry in `core/common/programs.py`. That registry
  stayed unused by deliberate choice (routes are mounted explicitly, by name, in
  `config/api_router.py`) — so once vitals/alerts left `core/`, `api_router.py` needed to
  import their views directly to mount routes at all. Reviving the registry just to satisfy
  this contract would have meant adopting indirection already rejected on its own merits.
  See the contract's replacement comment in `pyproject.toml` for the full reasoning.

### What genuinely does not exist yet

- **The NGO emergency-response portal.**
- **`.claude/skills/momcare-*`** — folders exist, content does not.
- **An obstetrician's review of the clinical thresholds** — the model's training data, the
  five category cutoffs (`momcare_model/clinical_categories.py`), and the escalation timings
  (`modules/pregnancy/alerts/escalation.py`) have not been checked by anyone with medical
  training. Stated
  as a hard requirement before real clinical use, not implied otherwise.

(RLS actually protecting production **used to** be listed here — it isn't anymore; see
Tenancy above, it's done as of 1 Sep 2026.)

### Deployment

**Live.** Frontend at `https://momcare.solutions` (Vercel, auto-deploys on push to
`main`), API at `https://api.momcare.solutions`. Confirmed 2026-08-31 via response
headers and by watching a push actually update the live site. Read `DEPLOY.md` before
touching anything deployment-related — every environment variable, and which failures
are silent. `../docs/deployment-plan.md` carries the audit findings; confirm against
the live site before trusting its "current blocker" as still current.


## Conventions & gotchas
- API ids are **UUID strings**; never assume integer ids.
- `test.py` never overrides `DATABASES` — tests run on the same Postgres engine as production.
- Pagination envelope: `{count, page, page_size, total_pages, next, previous, results}`
  (`core/common/pagination.py`, `DefaultPagination`). Any viewset with `ordering_fields` must
  use `StableOrderingFilter` from the same module.

### Sign-in is email only — decided 15 Sep 2026, do not re-add the alternatives

`/api/auth/login/` accepts `email` + `password`. Nothing else.

Neuro_RPM's multi-identifier login (one `identifier` field resolving to email, phone or
username by shape) was ported in full, with tests, and then **deliberately reverted the same
day**. `User.phone` is a plain `CharField` matched exactly, with no normalisation anywhere,
so of the eight ways a real person writes one Pakistani number — `+923001234567`,
`+92 300 1234567`, `03001234567`, `0300-1234567`, `00923001234567` and so on — **exactly one
logged in**: the byte-exact string stored at signup. `User.phone` is also `unique=True`, so
two spellings of the same number are two rows and the same woman can register twice.

Neuro_RPM has the identical bug (`User.objects.filter(phone=value)`); this is one of the few
places their implementation is simply wrong for a global product. Fixing it properly needs
E.164 normalisation via `phonenumbers`, which needs a default country to read a bare national
number — a guess this platform cannot make for a hospital it has never seen. Email has one
worldwide format.

`username` is collected by no form and should stay that way: it is unique-constrained so
duplicate names are not the obstacle, but a handle a rural mother never chose is a worse
identifier than the number she has had for years.

**Phone is still collected** — for patients, hospital owners and staff — as contact
information, so a hospital can reach a clinician about an alert off-portal. When adding it to
any new form, **convert blank to `NULL`**: `User.phone` is unique, so a second person stored
with `""` raises `duplicate key value violates unique constraint "users_user_phone_key"`.
`onboard_staff()` (`phone=phone or None`) and `StaffUpdateSerializer.update()` both guard
this, proven by fault injection.

---

## The clinical modules (added Aug 2026)

Read `../docs/PLAN.md` first — current status, decisions not to revisit, known gaps.

### Apps and what each owns

| App | Models | The thing to know |
|---|---|---|
| `staff` | `Staff` `SecondaryProvider` | `SecondaryProvider` is an **external** clinician — no login, no role, never in `/api/staff/`. It is a table rather than columns on Patient because one referring doctor is shared across many patients. Unlike the reference platform's version it carries an `organization` FK: Neuro_RPM is single-tenant, MomCare is not, and without it every hospital would read every other hospital's referral list. **`GET /api/staff/{id}/audit-report/`** (added 25 Sep 2026, inspired by a competitor's staff-activity dashboard — see `docs/design/2026-09-25-staff-audit-report-design.md`) is a read-only aggregation over `Staff`/`MonitoringSession`/`MonitoringNote`/`Alert` — caseload, monitoring time, call outcomes, alerts handled — for a preset rolling window (`?period=2d\|week\|month\|3month\|6month\|year\|2year`, no free-form dates). Deliberately has no RPM/CCM split or "compliance %" — neither concept exists in MomCare's single-programme model. Access reuses `can_manage_staff()` unchanged: self, hospital_admin, or a manager of at least one of the staff member's locations; anyone else in the same hospital gets 403, another hospital's `staff_id` gets 404. |
| `patients` | `Patient` `Pregnancy` `PatientJoinRequest` | `Patient.user` is **optional** (`SET_NULL`) — a rural patient may have no email and must still have a record. The care team is three direct columns on `Pregnancy` — `provider` (the accountable lead, what alert escalation routes to), `nurse`, `care_manager` — one of each at a time. **`Pregnancy` is the enrol→discharge episode** — there is no separate programme-enrollment table, and a `PatientProgramEnrollment` was deliberately removed for duplicating that role (see `patients/migrations/0011`). MomCare runs one programme; if a second ever arrives, that table earns its place back then. The seven obstetric-history answers and `Patient.consent_date` are plain columns, not satellite tables — `PregnancyRiskFactors` and `Consent` were folded in by `patients/migrations/0012`: one was answered only per-pregnancy, the other became a single date matching the reference platform. Consent is therefore **no longer mandatory** at onboarding. `PatientJoinRequest` is the self-registration path (Part B, built 2026-09-15): a woman registers in the app with `User.organization = NULL`, browses approved hospitals, and sends a request carrying a `draft` of her own details. **No `Patient` row exists until a hospital approves** — and approval calls the same `onboard_patient()` a walk-in uses, so there is exactly one creation path. Her own two endpoints run inside `bypass_rls()` with a `user=request.user` filter, because an org-less token makes the fail-closed policy hide her own rows from her. `ClinicalNote` was also removed (`patients/migrations/0013`) — notes are monitoring, not onboarding, and will be designed fresh alongside the reference platform's tags/templates/sessions rather than half-existing as a text field. **`PatientListSerializer` enriched 26 Sep 2026** with `gestational_age_long_display`, `provider_name`/`nurse_name`/`care_manager_name` (embedded from `current_pregnancy` — see "Why the care team lives on Pregnancy, not Patient" above), `language` (from `Patient.user.language`, null with no app account), `pending_risk_count`/`needs_risk_review`/`needs_low_confidence_review` (unconditional, on every row — see "Risk review workflow" below), and the three Care Activity signals (`last_monitoring_contact_at`, `last_reading_at`, `monitoring_seconds_this_month`) — all read-only display convenience, ownership unchanged. **`last_reading_display`/`last_monitoring_contact_display` added 27 Sep 2026** — "Today"/"Yesterday"/"N days ago", via a new `core/common/formatting.py::humanize_days_ago()` ported verbatim from Neuro_RPM's own function of the same name (`None` in, `None` out, so "never" stays visibly distinct from "today" rather than a fabricated value), resolved in the patient's own Location timezone. Additive alongside the raw `_at` timestamps, same "raw field stays, add a display convenience" pattern as `gestational_age_long_display`. **`monitoring_time_display` added 27 Sep 2026**, same pattern again: `format_duration()` (`core/monitoring/services.py`, already used for the Staff Audit Report) formats `monitoring_seconds_this_month` as "26m 3s"/"1h 15m 8s"/"7d 4h 45m 12s" instead of a bare integer. `GET /api/patients/` gained `?location=`/`?is_active=`/`?care_manager=`/`?provider=`/`?nurse=` filters the same day (`PatientScopedView.apply_roster_filters`, shared with `dashboard-kpis` below so the two can never disagree about scope) — the three staff filters join through `pregnancies__`, same mechanism `?assigned_to=me` already used. It also gained `?workflow=risk_review`/`low_confidence` and `?care_activity=monitoring_follow_up`/`unseen_readings`/`reading_reminder` (`PatientListCreateView._apply_workflow_and_care_activity`) — these **replaced five separate standing endpoints** that shipped first and were consolidated the same day; see "Risk review workflow" and "Care Activities" below for the full reversal and why. **`GET /api/patients/dashboard-kpis/`** is the single combined KPI surface, matching Neuro_RPM's own `dashboard-kpis` shape with MomCare's own workflows/care-activities: `{total_patients, active_patients, inactive_patients, pending_join_requests, workflow: {risk_review, low_confidence}, care_activities: {monitoring_follow_up, unseen_readings, reading_reminder}}`. No `priority_list`/`manage_careplans` keys (Neuro_RPM's Priority Patients was proposed and explicitly declined — see "Care Activities" below; MomCare has no CCM care-plan concept either). All five workflow/care-activity counts share the identical roster (`apply_roster_filters` + `?assigned_to=me`) the query-param filters use, via the same underlying condition functions — a number here can never disagree with what that filter actually returns. **`pending_join_requests` added 26 Sep 2026**, a top-level sibling rather than nested in `workflow`/`care_activities` — a `PatientJoinRequest` isn't a `Patient` yet (no row exists until a hospital approves it), so it fits neither shape; scoped only by organization, deliberately not run through `apply_roster_filters`/`?assigned_to=me` since a join request has no location/care-team assignment for those to narrow. No Neuro_RPM equivalent — MomCare's `PatientJoinRequest` self-registration flow (see this row's own entry above) has none in that codebase to port from. **`GET /api/patients/quick-lookup-kpis/` added 27 Sep 2026** (`PatientQuickLookupKpisView`), matching Neuro_RPM's own `quick-lookup-kpis`: `{staff: {total, active, inactive}, patients: {total, active, inactive}}`, organization-wide across every location, never narrowed by `?location=`/`?assigned_to=me` even if passed — the whole point is a total that doesn't move when a hospital admin changes their dashboard's location filter, unlike `dashboard-kpis` above. **Adapted for multi-tenancy, not ported literally**: Neuro_RPM's own version applies *zero* scoping (`Patient.objects.aggregate(...)`, no organization filter at all), because that codebase is single-tenant — one hospital per deployment, no `Organization` model to filter by. MomCare is shared-schema multi-tenant, so here "no location/role scoping" means skip `apply_roster_filters`/`?assigned_to=me` only; the organization filter itself stays mandatory — dropping it would be a cross-tenant PHI leak, non-negotiable regardless of what the reference platform's own single-tenant version does. |
| `modules/pregnancy/vitals` | `Device` `VitalReading` `RiskAssessment` | Readings attach to a **pregnancy**, not a patient — a heart rate of 110 means different things at 12 and 38 weeks. Lives under `modules/`, not `core/`, since 16 Sep 2026 (see "Devices, readings, risk and alerts" above) — the Django `app_label` is still `monitoring`, unchanged from its former `core/` home, so table names and RLS policies didn't move with it. **Reading statistics and Vitals Summary added 25 Sep 2026** (see `docs/design/2026-09-25-reading-statistics-design.md`), adapted from Neuro_RPM's own two independently-built features, both plain arithmetic — no AI/ML involved (that's `momcare_model`, a separate, unrelated feature). `GET /readings/` gained `period`/`start_date`+`end_date` window filters and a `reading_type` param that both scopes and triggers a `statistics` block (average/min/max/count/category-percentages) — no separate flag, matching Neuro_RPM's own implicit trigger. `reading_type` is one of `blood_pressure` (bundles `heart_rate` with it, matching Neuro_RPM's own grouping) / `temperature` / `blood_glucose` / `hemoglobin` / `wellness` — statistics are always scoped to exactly one group, never all 9 vitals at once, since `VitalReading`'s flat schema (unlike Neuro_RPM's per-vital-type tables) has no natural single-type filter otherwise. Category percentages classify raw readings directly via the existing `momcare_model/clinical_categories.py` functions, not `RiskAssessment` rows — a reading's vitals fall into a band regardless of whether that reading's own assessment changed the pregnancy's risk level. `allocate_percentages()` (`momcare_model/statistics.py`) is Neuro_RPM's largest-remainder rounding algorithm, ported verbatim. Displayed averages/min/max use `round_metric_value()` (same module) — **per-metric decimal places, not a uniform 2 decimals**: BP and heart rate round to the nearest whole number and return `int` (125.9 → `126`, never bucketed to a nearest-ten value like 130); temperature, glucose, hemoglobin and the two wellness scores keep 1 decimal and return `float`. `METRIC_ROUNDING` is Neuro_RPM's own convention, extended for the two vitals it doesn't have (hemoglobin, wellness scores → 1 decimal, matching its "continuous measurement" tier). New `GET /vitals-summary/` is the separate, simpler "quick glance" endpoint: fixed rolling 30-day average across every vital, no filters, same per-metric rounding — MomCare's version has none of Neuro_RPM's cross-table weighted-merge complexity, since `heart_rate` is one column here, not duplicated across two reading-type tables. Neuro_RPM's "previous period comparison" was deliberately not built (most complex piece, least obviously useful, nothing asked for it). **Risk review workflow (`review`/`escalate`/`bulk-review`, plus `?workflow=risk_review`/`low_confidence` on `GET /api/patients/`) added 26 Sep 2026** — see its own section below, "Risk review workflow — pending/reviewed/escalated". **`reassess_risk()` changed 27 Sep 2026 to write one `RiskAssessment` row per reading, unconditionally** — see "One `RiskAssessment` row per reading" above for the reversal of the original transitions-only design. `GET /vitals-summary/` gained a `risk_this_month` field the same day (`compute_month_risk_breakdown()`): percentage of Low/Medium/High among every `final_risk_level` this **calendar month** (Jan/Feb/Mar, not the endpoint's own rolling-30-day window used for `last_30_days_average`) — explicitly requested to match `monitoring_seconds_this_month`'s own calendar-month convention instead, and only practical to build without a model re-run because of the same-day per-reading storage change. `None` when there were no assessments that month, not an all-zero breakdown. |
| `modules/pregnancy/alerts` | `Alert` `AlertEvent` | The push side. `AlertEvent` is append-only: escalation not written down is escalation that never happened. Same `core/`→`modules/` move, `app_label` still `alerts`. |
| `core/monitoring` | `ClinicalTag` `MonitoringSession` `MonitoringNote` `StatusLabel` `PatientStatus` `NoteTemplate` | Built 23 Sep 2026 — clinical contact logging (calls, chart reviews, notes), adapted from the reference platform's own `core.monitoring` with three deliberate departures: no RPM/CCM program split (MomCare has exactly one programme, so `MonitoringSession` carries a single `duration_seconds`, not a per-billing-program breakdown); attaches to **`Patient`** with an optional **`pregnancy`** FK auto-filled from `patient.current_pregnancy` (not `Pregnancy` alone — `onboard_patient()` allows a patient with no pregnancy yet, unlike readings/alerts which always have one); `ClinicalTag` is tenant-scoped (`organization` XOR `location`, exactly one) where the reference platform's own tag list is global, because MomCare is multi-tenant and it isn't. A new `Location` auto-copies its hospital's org-level tags down as independent rows (`monitoring/signals.py`), the same `post_save` pattern the reference platform uses for `NoteTemplate`/`ChronicCondition`/`Medication`, applied here to a model that platform never scoped this way. **`app_label` is `clinical_notes`, not `monitoring`** — that label already belongs to `modules/pregnancy/vitals` (kept from its own former `core/` home), so this app, despite being the thing `core/monitoring`'s *name* was freed for, needed a different label. Endpoints are flat/patient-nested APIViews matching the rest of this project's convention, not the reference platform's `ModelViewSet`s. **`StatusLabel`/`PatientStatus` added 24 Sep 2026** (see `docs/design/2026-09-24-patient-statuses-design.md`), adapted from the reference platform's `GlobalStatus`/`PatientStatus`: `StatusLabel` is a hospital-invented, freely-colored status catalogue, scoped and copied to new locations exactly like `ClinicalTag`; `PatientStatus` is an append-only, editable/hard-deletable log entry on a patient (same shape as `MonitoringNote`, including `IsOwnerOrHospitalAdmin` on edit/delete). Two deliberate departures from the reference: no `(patient, name)` uniqueness — a status can recur over a pregnancy's months — and `PatientStatus` carries no FK to `StatusLabel` at all (matches the reference's own decoupling: the catalogue powers a picker, logging a status accepts free text regardless of what's in it). `PatientListSerializer`/`PatientDetailSerializer` embed a patient's **entire** status history (`name`/`description`/`color`, newest first) unconditionally, matching the reference platform's own unconditional embed rather than trimming to just the current entry. **`NoteTemplate` added 25 Sep 2026** (see `docs/design/2026-09-25-note-templates-design.md`), adapted from the reference platform's own `NoteTemplate`: reusable canned note text (`title`+`content`), scoped and copied to new locations exactly like `ClinicalTag`. Placed here rather than the reference platform's `organization` app — confirmed that placement there was pure historical migration baggage (a former standalone `core/notes` app folded in, table name pinned), not a meaningful design choice. **Confirmed via investigation that the reference platform never built a server-side "apply a template" mechanism** — no `template_id`/`source_template` field anywhere, no linkage to their note model at all; the frontend just copies a template's `content` into a new note's text field, and the resulting note is a plain, independent note with no record of which template (if any) it came from. `MonitoringNote` gets no new field for this. Two deliberate departures from the reference: `title` is unique per scope (the reference has no such constraint) — two templates with the same name in a picker is confusing, and this app's other catalogues already enforce it; no separate "for-location" dropdown endpoint — the plain list endpoint's existing `visible_*`-style scoping already answers the same question the reference platform needed a second endpoint for. |
| `core/analytics` | `PatientAnalytics` | Added 26 Sep 2026 — see "Care Activities" below for the full design. One model, `PatientAnalytics(patient, period_month, monitoring_seconds)`, deliberately narrower than the reference platform's own `PatientAnalytics`: only the genuinely calendar-month-scoped field lives here. `Patient.last_monitoring_contact_at`/`last_reading_at` are plain denormalized columns on `Patient` itself, not on this table — they're running "most recent" values with no month boundary, and putting them on a per-period row (as the reference platform does) is ambiguous about which period's copy is authoritative. Its own app (not folded into `core/monitoring` or `core/patients`) matching the reference platform's identical reasoning: it aggregates data owned by multiple other apps. Kept correct by signals in the *consumer*, not the source — `core/analytics/signals.py` listens to `MonitoringSession`/`MonitoringNote` (from `core/monitoring`), and a small addition to `modules/pregnancy/vitals/signals.py` updates `last_reading_at` on every `VitalReading` save — same ownership pattern `core/monitoring/signals.py` already uses for Location's tag/status/template copy-down (the app that needs the derived data owns the signal, not the app being observed). Recompute is always a full recompute from source data, never an incremental delta — `MonitoringSession`/`MonitoringNote` are editable and backdatable (unlike `VitalReading`), so a delta could silently drift from the truth; an edit that moves a record's `recorded_at` across a month boundary refreshes both the old and new month's rows via a `pre_save`-captured old value. Two more Care Activities followed the same day, both scoped to patients with an active pregnancy (unlike Monitoring Follow-up): `patients_with_unseen_readings` (a reading arrived after the last monitoring contact, within `UNSEEN_READINGS_WINDOW_DAYS`) and `patients_needing_reading_reminder` (no reading in `READING_REMINDER_GAP_DAYS`), both reusing `last_reading_at`/`last_monitoring_contact_at` with no new model fields or migrations needed. |

### The risk model lives outside `momcare_platform/` entirely

`momcare_model/` is a **separate top-level package at the repo root**, sibling to
`momcare_platform/` and `config/` — not a subfolder of either. `import-linter`'s
`momcare_model stays framework-free` contract (`pyproject.toml`) forbids it from ever
importing `django` or `momcare_platform`; it's plain Python + scikit-learn/XGBoost/joblib.

- `momcare_model/predict.py::predict(vitals: dict) -> dict | None` — the single inference
  entry point. XGBoost, 3-class (Low/Medium/High), **88.4% test accuracy**. Returns `None`
  (never a fabricated guess) only when every one of the 9 features is missing.
- `momcare_model/clinical_categories.py` — the five display-only vital categories
  (`bp_category` etc.). Never fed back into the model.
- `momcare_model/config.py` — the single source for `FEATURE_COLS` (order matters — it's
  the exact training column order), the `Low=0/Medium=1/High=2` encoding, and
  `ACTIVE_MODEL_VERSION` (currently `"v1"`, the only version ever trained).
- `momcare_model/train.py` / `evaluate.py` — dev-only, require the `ml-train` uv dependency
  group (`pandas`, `imbalanced-learn`), never imported by the running server.
- Artifact lives in `momcare_model/models/artifacts/v1/` (`xgboost_model.json`,
  `imputer.joblib`, `metadata.json`), loaded once per process and cached module-level.

**This entirely replaced the old rules engine.** `core/monitoring/risk_rules.py` (the
if/then threshold module this section used to describe) has been deleted — it does not
exist anywhere in the codebase anymore, and there is no if/then fallback if the model
can't produce an answer; `predict()` just returns `None`.

Two other framework-free modules, both pure functions, tested in milliseconds:

- `modules/pregnancy/alerts/escalation.py` — the tier ladder and its deadlines.
- `core/common/regions.py` — country → model region.

None of the model's training data, the five category cutoffs, or the escalation timings
have been reviewed by a practising obstetrician. That is stated in the docs as a
requirement before real use — do not quietly imply otherwise.

### Model region is derived, never asked for

`core/common/regions.py::region_for_country()`. The risk model is trained per
population (`asia`, `africa`, `americas`), and the region comes from the country
onboarding already requires — reachable as `Organization.region` and
`Pregnancy.region`.

**Do not add a region field to any form or model.** A second answer can
contradict the first — a hospital in Lahore filed under Africa — and the model
would be handed a population it was not trained on. Same rule as gestational age
below: one function, derived on read, corrections flow through automatically.

A country the model has no data for returns **`None`**, not a default. The 37
such countries the onboarding form offers are listed explicitly in
`_OUT_OF_SCOPE` so that "we decided this is unsupported" is distinguishable from
"someone forgot"; adding a country to the form's dropdown fails the test suite
until its region is decided.

The mapping is duplicated in `frontend/src/features/hospital-onboarding/regions.ts`
so the form can show the region as a country is picked. That copy is
display-only and never submitted, and a test parses it and fails if one country
disagrees. **If you edit one, edit both.**

### Gestational age has exactly one home

`core/common/obstetrics.py::calculate_gestational_age()`. Derived from EDD on every
read, **never stored** — a stored column is wrong the next day. Never recompute it
anywhere else, or the list, the chart and the risk engine will disagree about how
pregnant someone is.

A second, dashboard-friendly display exists alongside the clinical "28w 3d" form:
`gestational_age_long_display()` (same module) and `Pregnancy.gestational_age_long_display`
render "7 months 2 weeks 4 days" — a "month" here is a 4-week (28-day) unit, not a
calendar month, chosen because `PREGNANCY_LENGTH_DAYS` (280 = 28 × 10) divides evenly by
it and a calendar month (28-31 days) does not. This is **additive, not a replacement** —
the short weeks+days form stays the clinical convention everywhere else (Pregnancy
detail, the risk engine, reports); the long form exists only on `PatientListSerializer`
for a lay-readable dashboard summary.

### Why the care team lives on Pregnancy, not Patient

`Pregnancy.provider`/`nurse`/`care_manager` — not `Patient` — even though Neuro_RPM
assigns staff to its Patient directly. This is not an inconsistency to fix; it follows
from the same reasoning as gestational age above, applied to accountability instead of
vitals interpretation.

Neuro_RPM monitors people with **ongoing, unbounded conditions** (diabetes,
hypertension) — there is no natural episode boundary, so "who is this patient's
provider" is a single, continuously-true fact with nothing that could silently
overwrite it later. Maternal health is structurally different: **pregnancy is a
bounded, repeatable episode.** The same woman can have several pregnancies at the same
hospital, and `provider` is "the accountable lead, what alert escalation routes to"
(see the `patients` app row below) — a historical fact about who was responsible for
*that pregnancy's* alerts, not a standing fact about the woman in general.

If care team lived on `Patient` instead: a woman reassigned to a different provider for
her *second* pregnancy would retroactively change who's on record as having been
accountable for her *first* pregnancy's alerts the moment that reassignment happened —
silently rewriting a historical accountability fact for an unrelated, earlier episode.
Keeping it on `Pregnancy` freezes that fact correctly. Confirmed directly against
Neuro_RPM's actual code before writing this: their `PatientSerializer.to_representation()`
merges `compute_dashboard_analytics()` (reading days, monitoring seconds, last
call/reading, RPM/CCM progress) onto every patient row unconditionally, and further adds
`priority_score` only when `?workflow=priority_list` is requested — so their own patient
list isn't uniformly-shaped either; the two systems just draw the line differently based
on what each domain's data actually needs.

**The solution for showing care team on the frontend is embedding, not moving the
data** — the same "ownership stays on Pregnancy, the Patient row gets a flattened
read-only copy" pattern `gestational_age_display` already uses. See `PatientListSerializer`
below.

### Scoping paths

```
Patient       → location__organization          (never user__organization)
Pregnancy     → patient__location__organization
Reading       → pregnancy__patient__location__organization
Alert         → pregnancy__patient__location__organization
RiskAssessment → pregnancy__patient__location__organization   (same path as Reading/Alert)
Staff         → user__organization
SecondaryProvider → organization            (direct column, like Device)
PatientJoinRequest → organization           (direct column; patient side uses bypass_rls + self-filter)
Device        → organization
OrganizationDeactivationRequest → organization    (direct column, like Device)
MonitoringSession → patient__organization     (Patient, not Pregnancy — see core/monitoring app row above)
MonitoringNote    → patient__organization
ClinicalTag       → organization XOR location__organization   (exactly one column set; see core/monitoring app row)
StatusLabel       → organization XOR location__organization   (exactly one column set; same shape as ClinicalTag)
PatientStatus     → patient__organization     (same shape as MonitoringNote; see core/monitoring app row)
NoteTemplate      → organization XOR location__organization   (exactly one column set; same shape as ClinicalTag)
Notification      → organization            (direct column, like Device)
PatientAnalytics  → patient__organization     (same shape as MonitoringNote; see core/analytics app row)
```

The one deliberate exception: `platform_admin`'s own views (`core/platform_admin/`) read
`OrganizationDeactivationRequest` **without** this scoping — they're cross-tenant by design
(a platform admin looks across every hospital at once) and run inside `bypass_rls()`
instead, the same sanctioned path `escalate_alerts` and Django admin already use.

Scope **before** lookup, so another tenant's row resolves to nothing. Cross-tenant
reads return **404, never 403** — a 403 confirms the record exists elsewhere.

### Risk scoring has exactly one producer — the model landed

There is no `source` column on `RiskAssessment` at all (an earlier version of this file
described one, planned for when the model landed vs. the old rules engine — that never
shipped, because the rules engine was deleted outright instead of kept as a second
producer). `confidence` is a real float on every row written today; it would only be null
on a row that predates the model, and there are none of those in a fresh database.

Two postprocessing steps run on the model's raw answer, in Python, never in the database
— see `MEMORY.md` §6 for the full reasoning:

- **Africa + Medium → shown as High.** `risk_level` keeps the model's real answer;
  `final_risk_level` is what's shown and acted on.
- **Below `Organization.effective_confidence_threshold`** (platform default **0.800**,
  raised from 0.700 on 2026-09-07 — `settings.MOMCARE_DEFAULT_CONFIDENCE_THRESHOLD`) sets
  `flagged_for_review = True`.

**As of the 13 Sep 2026 revision, `flagged_for_review` is flag-only — no email fires for
it, and no email fires for an alert either.**
`modules/pregnancy/alerts/services.py::notify_low_
confidence()` and `core/common/mail.py::send_alert_notification()`/
`send_low_confidence_notification()` were all deleted (commit `da94b70`), along with the
`MOMCARE_ALERT_EMAILS_ENABLED` setting that used to gate them. Reason, from that commit:
one Resend account is shared by every hospital with no separate staging backend, so a
clinical alert firing on every threshold crossing during testing or model development
would spend the same quota a real emergency needs. The in-portal alert (and the
`AlertEvent` audit trail recording who was notified) is unaffected — only the email leg
is gone, permanently, by design. **Do not re-add an email leg for alerts or low-confidence
flags without re-reading that commit message first** — this is not an oversight to "fix".

### One `RiskAssessment` row per reading — reversed 27 Sep 2026, do not re-narrow this

`reassess_risk()` writes a row for **every** reading, unconditionally — never re-running
`predict()`, just never skipping the write either. This reverses the original "transitions
only" design (a row written only when `final_risk_level` changed from the pregnancy's last
assessment), which held from the model's launch until this date and was described
elsewhere in this file and in `MEMORY.md` as settled. It was not a bug fix — the user
explicitly decided the complete per-reading clinical record matters more than the smaller
table, overriding the tradeoffs laid out for them: a much larger `risk_assessments` table
over time, and a bigger `pending_risk_count`/Risk Review Queue/Low Confidence Queue per
patient once a patient sits at an unreviewed actionable or flagged level for many
consecutive readings (each now its own pending row, not one row covering the whole
stretch) — `bulk-review` is the intended way to clear a run of these at once, not a
second design change layered on top.

`previous_risk_level` still records what the level was immediately before each row, so a
transition is always recoverable by comparing consecutive rows even though every row is
now stored — nothing about *reading* the history changed, only *how much* of it exists.
`sync_alert_for()` needed no change: it already keys off the *current* alert's own state
(`live.level`, not "did the assessment change since last time"), so calling it on every
reading — including many consecutive unchanged-level ones — is already idempotent: an
unchanged actionable level just repoints the live alert's `assessment` FK to the latest
evidence, no re-notify, no clock reset.

### Risk review workflow — pending/reviewed/escalated

Added 26 Sep 2026, adapted from Neuro_RPM's own Reading Review workflow
(`PatientReading.ReviewStatus`/`review()`/`escalate()`/`bulk-review`), with the trigger
swapped from their configurable `DataBound` thresholds to MomCare's own trained-model
confidence — MomCare deliberately has no thresholds rules engine (see "no rules engine" in
`MEMORY.md`), so `flagged_for_review` (confidence below `effective_confidence_threshold`)
is the only trigger, not a second concept layered on top of it.

`RiskAssessment.review_status` is `pending` / `reviewed` / `escalated` — renamed from
`unreviewed`/`confirmed`/`corrected` to match Neuro_RPM's naming exactly. Those old names
encoded a *different* fact (whether the doctor agreed with the model) than triage urgency
does; that fact isn't lost — it's still derivable by comparing `confirmed_risk_level` to
`final_risk_level` — it's just no longer what the status name itself encodes. Which
terminal state an assessment lands in is chosen by which endpoint the clinician calls
(`review` or `escalate`), not derived from agreement.

Endpoints (`modules/pregnancy/vitals/api/views.py`), both `IsClinician`-gated, both
requiring `confirmed_risk_level` in the body — that requirement is MomCare's own, kept from
the old `VerifyRiskView`, and deliberately **not** relaxed to match Neuro_RPM's plain
label-only actions: there is no "just seen, not confirmed" state here:

- `POST /pregnancies/{id}/risk/{assessment_id}/review/` — mark reviewed.
- `POST /pregnancies/{id}/risk/{assessment_id}/escalate/` — mark escalated. **A label
  only** — no Alert side effect, no notification — matching what Neuro_RPM's own
  `escalate()` actually does (nothing beyond the status; their own code comment admits the
  richer "Escalation workflow" it was meant to feed was never built). MomCare's real
  escalation ladder (`Alert`/`AlertEvent`, tiers, `escalate_alerts` cron) already runs
  independently of this field — confirmed with the user rather than wired together, since
  a flagged assessment doesn't always have a live Alert to act on (a flagged Low-risk
  reading never raises one at all — see `Alert`'s own "at most one open alert" rule).
- Both guard on `RiskAssessment.needs_attention` — **actionable (Medium/High) OR
  flagged_for_review, or both** — plus still-pending. Broader than Neuro_RPM's own
  `resolve_reading()` guard (theirs has a single axis, raw-value severity; MomCare has two
  independent signals, severity and model confidence, since only MomCare runs a real
  probabilistic model here). Calling either action on an assessment that needs neither, or
  one already resolved, is a 400.
- `POST /risk/bulk-review/` — resolve several assessments to their own target status in
  one atomic call, ported from Neuro_RPM's `bulk-review`. All-or-nothing: one bad item
  rolls back every write.

All three (`review`/`escalate`/`bulk-review`) are gated to **clinicians (Provider/Nurse/
Care Manager) or hospital_admin** — `IsClinician | IsHospitalAdmin`. Admin access was
added after a permission audit (requested by the user, comparing MomCare's role model
against Neuro_RPM's role-by-role) found a real, unintentional gap: these views had reused
the plain `IsClinician` class (which excludes admin) from the old `VerifyRiskView`, but
`IsClinician`'s own reasoning was written specifically for *Alert acknowledgment* (an
admin silencing the escalation ladder the instant it would reach them) — a concern that
doesn't transfer to resolving a `RiskAssessment`'s own `review_status`, which never
touches the Alert's clock. Matches Neuro_RPM's own permission for the identical action
exactly (`MANAGE = IsAdmin | IsCareManager` on their reading review/escalate/
bulk_review) — additive relative to Neuro_RPM, not a narrowing: Provider/Nurse keep the
access they already had, only hospital_admin's access was ever missing. Alert acknowledge/
resolve itself (`modules/pregnancy/alerts/api/views.py`) is unchanged — still
`IsClinician`-only, since that reasoning is still valid there.

The same audit found a second gap, this time in `core/patients`: `PatientDeactivateView`/
`PatientReactivateView` had inherited the base `PatientScopedView`'s plain
`IsHospitalStaff` (any staff — correct for read/create/update), letting a Provider or
Nurse take a patient off the active roster unilaterally. Neuro_RPM's identical action
gates to `MANAGE = IsAdmin | IsCareManager`; MomCare's two views now use
`IsHospitalAdmin | IsCareManager` to match. Most of the rest of the audit — Organization
settings, Location create/reactivate/update/deactivate, StatusLabel/ClinicalTag/
NoteTemplate writes, Staff onboarding, Users/Auth — was already correct (several had
already been built to explicitly match Neuro_RPM's own split in an earlier session; see
`core/locations/api/views.py`'s own module docstring). A follow-up pass (26 Sep 2026)
went through every remaining area — Staff CRUD (profile edit/delete, deactivate/
reactivate), `SecondaryProvider`, `core/monitoring` session/note edit-delete, Device
list/create/assign, and Alert list/detail/acknowledge/resolve — and found no further
gaps: Staff and `SecondaryProvider` were already confirmed correct in an earlier
session; `MonitoringSessionDetailView`/`MonitoringNoteDetailView`'s `IsOwnerOrHospitalAdmin`
matches Neuro_RPM's own `IsOwnerOrAdmin` on both its `MonitoringSessionViewSet` and
`MonitoringNoteViewSet` exactly; Device views' `IsHospitalStaff` matches Neuro_RPM's
`DeviceViewSet`/`DeviceEnrollmentViewSet` (`IsAdmin | IsProvider | IsCareManager |
IsNurse`, i.e. any staff role); Alert list/detail's `IsHospitalStaff` matches the same
any-staff set on Neuro_RPM's `AlertViewSet`, and Alert acknowledge/resolve's
`IsClinician`-only restriction (see above) was already a deliberate, unchanged decision
from the first pass. `platform_admin` remains a deliberate, documented stub (see
"core/ vs modules/" above) with nothing to audit yet. Two permission gaps total were
found and fixed across the whole audit — the risk-review actions and patient
deactivate/reactivate, both above.

**Two independent conditions, not one combined list** — **severity** (actionable,
Medium/High, regardless of confidence) and **confidence** (flagged for low model
confidence, regardless of level — a flagged Low counts). A patient can match both. Both
resolve through the identical `review`/`escalate`/`bulk-review` actions, so nothing about
*acting* on an entry duplicates between the two, only the *listing* condition differs.

A patient stays listed for **any** outstanding pending match, not only their current
(latest) assessment — every reading gets its own `RiskAssessment` row (see "Risk scoring
has exactly one producer" below for the one-row-per-reading reversal), and an older,
never-touched row keeps counting even after a newer reading has already superseded it.
Matches Neuro_RPM's own `reading_review_patient_condition()` exactly: "a pending reading
from any past month still counts until it is reviewed/escalated." Each result's `pending_risk_count`
says how many qualifying assessments are still outstanding for that patient — a manual
30-reading, 3-patient end-to-end run (requested explicitly after the two-condition split
shipped) briefly led to a wrong "fix" narrowing this to current-state-only, which was
reverted the same day once cross-checked against the Neuro_RPM precedent above.

`GET /pregnancies/{id}/risk/` (the assessment history endpoint) accepts
`review_status`/`actionable`/`flagged_for_review` query params on `history` — `current` is
always the true current assessment, unaffected. This mirrors Neuro_RPM's real pattern:
their frontend gets "this patient's outstanding out-of-range readings" from their generic
reading list's own filters (`filterset_fields = ["patient", "reading_type",
"is_out_of_range"]` plus their hand-rolled `review_status`), not a bespoke per-workflow
endpoint.

**Exposed on the API as `?workflow=risk_review`/`?workflow=low_confidence` on
`GET /api/patients/`, not as separate standing endpoints — this was a deliberate reversal,
not the original design.** The two conditions first shipped as `GET /risk-review-queue/`
and `GET /low-confidence-queue/`, each with its own `-kpis` sibling — five endpoints
total once Care Activities joined them (see below), explicitly justified at the time by
matching `AlertListView`'s own standing-endpoint convention and by each queue needing
different extra per-row data (`pending_risk_count`, `reasons`, a full embedded
`assessment` object) that a single shared response shape couldn't hold cleanly. The user
pushed back hard on this, pointing out that Neuro_RPM's own equivalent care activities
(`monitoring_follow_up` included) are query-param filters on their one Patient List
endpoint, not separate URLs — and after direct verification against Neuro_RPM's actual
code, the "different response shapes" objection didn't hold up either: their own
`PatientSerializer.to_representation()` unconditionally merges `compute_dashboard_
analytics()` (reading days, monitoring seconds, last call/reading, RPM/CCM progress) onto
**every** patient row regardless of filter, and conditionally adds `priority_score` only
for `?workflow=priority_list` — their shape isn't uniform either, it's just merged onto
every row rather than varying by filter. Resolved the same way: `PatientListSerializer`
now unconditionally carries `pending_risk_count`/`needs_risk_review`/
`needs_low_confidence_review` on every row (whether or not `?workflow=` is passed), the
full per-assessment `reasons` array and embedded `assessment` object were dropped from
the merged shape (available in full via the unchanged `GET /pregnancies/{id}/risk/`
history endpoint instead), and the five standing endpoints — along with their five
`-kpis` siblings — were deleted outright. `review`/`escalate`/`bulk-review` were
unaffected by this move: those are POST actions in Neuro_RPM too, never query params, so
only the GET/listing side was ever in scope.

The condition logic itself didn't change — `patients_needing_risk_review()`/
`patients_needing_low_confidence_review()` (`modules/pregnancy/vitals/services.py`,
rooted on `Patient` via the `pregnancies__` join rather than `Pregnancy` directly, since
the caller is now always the patient list) are the exact same filters the old
`RiskReviewQueueView`/`LowConfidenceQueueView` used. `PatientListCreateView.
_apply_workflow_and_care_activity()` (`core/patients/api/views.py`) resolves them via
`importlib`, not a static import — they live in `modules.pregnancy.vitals`, which `core`
must never import. `GET /api/patients/dashboard-kpis/`'s `workflow.risk_review`/
`workflow.low_confidence` counts reuse the identical functions, so a number there can
never disagree with what `?workflow=` actually returns.

Neither condition is a Neuro_RPM port, regardless of how it's exposed. Confirmed directly
against their code: their similarly-shaped "Reading Review" and "Out of Range" dashboard
tiles are **not** a severity/confidence split — both read the identical `is_out_of_range`
signal (`DataBound`, their configurable raw-value threshold), differing only in time
window. Neuro_RPM has no confidence-score concept anywhere in this system at all, since
they have no trained model gating it. This dual-condition problem is MomCare-specific;
only the decision to expose it as a query-param filter rather than a standing endpoint is
a Neuro_RPM port.

### Care Activities — live roster signals, distinct from the review-status workflows above

Added 26 Sep 2026, `core/analytics`. A **Care Activity** is not a workflow: no status
field, no review/escalate action, no history. It's a live, stateless snapshot —
"does this condition hold for this patient right now" — recomputed on every read, not a
queue with a decision to record. This is a genuinely different mechanism from Risk Review
Queue/Low Confidence Queue above, even though both can be fed by related raw signals:
Neuro_RPM's own "Reading Review" workflow (the direct ancestor of MomCare's two risk
queues) queries `PatientReading.review_status`/`is_out_of_range` **directly on the
reading**, and never touches its separate `PatientAnalytics` cache at all — that cache
backs only Neuro_RPM's four **Care Activities** (`out_of_range`, `monitoring_follow_up`,
`unseen_readings`, `reading_reminder`), a dashboard-tile/roster-filter concept, not an
audit trail. The two mechanisms don't even share membership: a reading already reviewed
(workflow closed) can still show in the Out of Range Care Activity tile for the rest of
its day window, since that filter doesn't know or care about `review_status`.

**Monitoring Follow-up is the first Care Activity MomCare has built.** It flags a
patient staff haven't meaningfully checked on: **less than 20 minutes of monitoring time
logged this calendar month AND no monitoring contact (session or note) in the last 2
days** — both conditions must hold, matching Neuro_RPM's own `monitoring_follow_up`
condition (`core.patients.services.MONITORING_FOLLOW_UP_MAX_SECONDS`/
`MONITORING_FOLLOW_UP_MIN_GAP_DAYS` in that codebase) exactly, constants hardcoded, no
per-org setting. The 20-minute figure is Neuro_RPM's own CMS/CPT-99457 billing minimum,
reused here **only as a plain heuristic number**, never as a billing computation —
confirmed explicitly with the user before building it in, since MomCare has no billing
module and has already decided against an RPM/CCM program split (see `core/monitoring`'s
own row above). A patient with no analytics row yet for the current month implicitly
qualifies (0 seconds, no session) — never a reason to exclude her, matching Neuro_RPM's
own "missing row means she qualifies" behavior. No `review`/`escalate` action exists for
it and none should be added — the condition resolves itself the instant a new
session/note makes it false, same as the model landing on it decided against building one
for Alert-adjacent reasons.

**Out of Range was proposed as a second Care Activity and explicitly rejected.**
Neuro_RPM's `out_of_range` depends on `DataBound`, a raw configurable numeric threshold
on the reading itself — the exact rules-engine mechanism MomCare deleted outright when
the trained model shipped (`core/monitoring/risk_rules.py`, see "Risk scoring has exactly
one producer" below). Risk Review Queue **is** MomCare's version of that same clinical
question, answered by the model instead of a hand-set threshold; building a literal Out
of Range Care Activity on top would mean resurrecting the rules engine through the back
door. **Unseen Readings and Reading Reminder were built the same day** (`core/analytics/
services.py`'s `patients_with_unseen_readings`/`patients_needing_reading_reminder`) —
neither depends on `DataBound`, so neither has this conflict. Unseen Readings: a reading
arrived after the last monitoring contact, within the last `UNSEEN_READINGS_WINDOW_DAYS`
(7) — compares `Patient.last_reading_at` to `Patient.last_monitoring_contact_at` (did a
human look since a new reading arrived). Reading Reminder: no reading in the last
`READING_REMINDER_GAP_DAYS` (3) — `Patient.last_reading_at` alone (has data stopped
arriving at all). Complementary to Monitoring Follow-up's staff-side question, not
redundant with it or with either risk queue. **Both are scoped to patients with an
active pregnancy**, unlike Monitoring Follow-up — a `VitalReading` always requires one
(unlike `MonitoringSession`/`MonitoringNote`, which don't), so a patient with none can
never have a reading and would just be noise in these two lists.

**Priority Patients was proposed as a fourth workflow and explicitly rejected.**
Neuro_RPM's `priority_score = P(new) + P(off-track) + P(not-called) + P(compliant)` is a
billing-cycle compliance score, not a simple "who needs attention" ranking — `P(off-track)`
depends on which half of the calendar month it is relative to CMS billing phases, and
`P(compliant)` is literally a percentage of billing requirements met that cycle (the same
33/34/33 reading-days/TWC/time split as RPM Progress). This is a much deeper billing
entanglement than Monitoring Follow-up's single borrowed 20-minute constant — it's an
entire scoring formula built around a monthly billing cycle MomCare doesn't have. Not
ported; no MomCare equivalent exists, and `dashboard-kpis` below has no `priority_list` key.

**The combined `dashboard-kpis` consolidation — the item this whole "Care Activities"
section originally deferred until all workflows existed — was built the same day**, once
the user asked for it directly (see the `patients` app row above for the exact shape).
Matches Neuro_RPM's own single-endpoint convention, with MomCare's three workflows/
care-activities instead of their four/two.

**Schema is deliberately narrower than Neuro_RPM's own `PatientAnalytics`.** Only
`monitoring_seconds` (calendar-month-scoped, resets every period) lives on the new
`PatientAnalytics(patient, period_month, monitoring_seconds)` model.
`last_monitoring_contact_at` and `last_reading_at` are plain denormalized columns
directly on `Patient` instead — running "most recent" values with no month boundary, so
putting them on a per-period row (as Neuro_RPM does, ambiguously — its own schema
duplicates a "last" value across every month's row) would just create a question of which
period's copy is authoritative. Both new `Patient` columns and the `monitoring_seconds`
column were built together in the first pass, all correctly wired to their signals from
day one, even before Unseen Readings/Reading Reminder had queries consuming
`last_reading_at` — no placeholder/dead columns at any point.

Recompute is a full recompute from source data, **never an incremental delta** —
`MonitoringSession`/`MonitoringNote` are editable and backdatable (unlike `VitalReading`,
which is neither), so a delta could silently drift from the truth. An edit that moves a
record's `recorded_at` across a month boundary refreshes both the old and new month's
rows, via a `pre_save` receiver that stashes the old value before the write lands. Lives
in the *consumer* app (`core/analytics/signals.py` listening to `core.monitoring`'s
models, plus a small addition to `modules/pregnancy/vitals/signals.py` for
`last_reading_at`), not the source — matching `core/monitoring/signals.py`'s own existing
precedent (Location's tag/status/template copy-down is likewise owned by the app that
needs the derived data, not the app being observed).

**Exposed as `?care_activity=monitoring_follow_up`/`unseen_readings`/`reading_reminder`
on `GET /api/patients/`, not as separate standing endpoints — same reversal, same day, as
the risk queues above.** All three first shipped as their own URLs
(`GET /monitoring-follow-up-queue/`, `GET /unseen-readings-queue/`,
`GET /reading-reminder-queue/`, each with a `-kpis` sibling), sharing one
`_CareActivityQueueBase` (`core/analytics/api/views.py`, since deleted) differing only in
`base_patients()` (which patients are even eligible) and `condition()` (which of them
currently qualify). Collapsed into query params for the identical reasoning covered under
"Risk review workflow" above — see that section for the full account, including the
Neuro_RPM-verification that corrected an overstated claim about response-shape
uniformity. `patients_needing_monitoring_follow_up()`/`patients_with_unseen_readings()`/
`patients_needing_reading_reminder()` (`core/analytics/services.py`) are unchanged by the
move — plain static imports from `core/patients/api/views.py` since `core.analytics` is
itself a core app, no `importlib` needed the way the risk conditions require. No
review/escalate endpoints on any of them, by design — a Care Activity has no status to
resolve.

### Scoring and alerting are one transaction

`reassess_risk()` writes an assessment for every reading (see "One `RiskAssessment` row
per reading" above), then calls `alerts.services.sync_alert_for()`. An assessment saying
"critical" with no alert is a state this system must not be able to reach.

Both imports are function-local: `alerts` imports `monitoring`, so a module-level
import the other way closes the cycle.

### Escalation needs a scheduler

Alerts are *raised* inside the request that recorded the reading. They only *climb*
when `manage.py escalate_alerts` runs — cron or Task Scheduler, every minute.
Idempotent and safe to run late: the target tier is computed from the clock, so a
missed hour lands on the right rung instead of stepping up once per missed run.

### Things the admin must never allow

No delete for organizations, patients, pregnancies, readings, assessments or alerts.
Readings and assessments are also not editable — an observation of a moment in time
is not editable; a correction is a new reading.

### Testing

```bash
uv run pytest momcare_platform/core -q      # 715 passed, 4 skipped, as of 27 Sep 2026
```

Mostly **API-level integration tests** — a real request through routing, middleware,
JWT, permissions, serializer and a real Postgres. Nothing mocked. `momcare_model`,
`escalation.py` and `regions.py` are tested as pure functions, with no Django involved.

Security tests were validated by **fault injection** — deliberately removing each
protection and confirming the tests failed. If you add a protection, prove its test
fails without it.

`conftest.py` clears the throttle cache between tests. Without it the suite shares
one `100/day` anon bucket and starts returning 429 once enough tests have logged in.

### Demo helper

`manage.py demo_setup [--reset-alerts]` — known passwords for a walkthrough.
Refuses to run with `DEBUG` off. **Skips superusers and platform admins by default**;
never pass `--include-admins` unless the user asks for exactly that.
