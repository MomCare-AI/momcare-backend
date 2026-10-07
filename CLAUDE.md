# CLAUDE.md

Guidance for Claude Code / any AI agent on this repo. **This is the condensed version.**
The full original (all history, reasoning, reversals, Neuro_RPM comparisons) is preserved
unchanged in [`docs/CLAUDE-reference.md`](docs/CLAUDE-reference.md) — read the relevant
section there *before* changing anything listed under "Read the reference before…" below.
Method/workflow rules are in `AGENTS.md`. Plan/status: `../docs/PLAN.md`. Deploy: `DEPLOY.md`.

## Overview

`momcare_platform` is a **backend-only, API-only** Django suite for maternal-health B2B SaaS.
Hospitals register, then onboard their own staff and patients. No server-rendered UI (only
Django admin + Swagger). A separate SPA frontend talks to `/api/`. Follows the Universal
Modular Monolith Blueprint (sibling reference project: Neuro_RPM).

## Tenancy — read before touching any model

**Shared-schema multi-tenant**: many hospitals, one DB. `Organization` = one row per hospital.
Every tenant-owned model carries an `organization` column (directly or via `Location.organization`).

- **App level**: compose `OrganizationScopedQuerysetMixin` / `LocationScopedQuerysetMixin`
  (`core/common/scoping.py`) into **every** viewset over tenant data. "Did I compose the
  scoping mixin" is a mandatory review item — a miss is a cross-hospital PHI leak.
- **DB level**: Postgres RLS on every tenant table, fail-closed (unset session var = zero rows).
  **Enforced in production** (since 1 Sep 2026): `DATABASE_URL` = restricted `momcare_app`
  role (NOBYPASSRLS, no DDL); `MIGRATION_DATABASE_URL` for `migrate`/`createcachetable`
  (switched by command-name detection in `config/settings/production.py`, not the Procfile).
  Local/test use one `BYPASSRLS` role, so RLS isn't exercised by normal runs — use
  `scripts/verify_rls.py`. `TenantAwareJWTAuthentication` (`core/users/api/auth.py`) resolves
  identity under RLS.
- `Patient` has **both** `location__organization` (the scoping path) and a direct `organization`
  FK (for per-hospital national-ID uniqueness across branches). Do not "simplify" it away.
- Scope **before** lookup; cross-tenant reads return **404, never 403**.

### Scoping paths
```
Patient → location__organization (never user__organization)
Pregnancy / Reading / Alert / RiskAssessment → pregnancy__patient__location__organization
  (Pregnancy: patient__location__organization)
Staff → user__organization
SecondaryProvider, PatientJoinRequest, Device, OrganizationDeactivationRequest, Notification → organization
MonitoringSession, MonitoringNote, PatientStatus, PatientAnalytics → patient__organization
ClinicalTag, StatusLabel, NoteTemplate → organization XOR location__organization (exactly one set)
```
Only exception: `platform_admin`'s views are cross-tenant by design and run in `bypass_rls()`
(same sanctioned path as `escalate_alerts` and Django admin).

## Roles — two-tier
- **Platform**: `platform_admin` (org null, bypasses scoping).
- **Hospital**: `hospital_admin`, `provider`, `nurse`, `care_manager`, `patient` (one `User.organization`).
- Use `settings.ROLE_*` constants matched against `User.role.code` — never hardcode role strings.

## Commands (everything via `uv`; default settings `config.settings.local`)
```bash
uv sync
uv run python manage.py migrate | createsuperuser | runserver
uv run pytest [--create-db]               # full suite, real Postgres (~878 tests, 4 skipped)
uv run pytest momcare_platform/core -q    # faster core subset (~715)
uv run ruff check . ; uv run ruff format .
uv run mypy momcare_platform
uv run lint-imports                       # import-linter contracts — run as standard verification
uv run pre-commit run --all-files
```
URLs: `/admin/`, `/api/`, `/api/docs/` (Swagger, admin login), `/health/` (also `/`).

## Architecture
- `momcare_platform/core/` — `common` (no models: base classes, permissions, scoping, gating,
  middleware), `users`, `organization`, `locations`, `staff`, `patients`, `monitoring`
  (app_label **`clinical_notes`**), `analytics`, `platform_admin` (**empty placeholder on
  purpose — do not build its API unless asked**; org/deactivation review is via Django admin).
- `momcare_platform/modules/pregnancy/` — `vitals` (Device, VitalReading, RiskAssessment;
  app_label **`monitoring`**, unchanged so table names/RLS didn't move) and `alerts`
  (Alert, AlertEvent; label `alerts`).
- `momcare_model/` — separate top-level, **framework-free** package (no django / momcare_platform
  imports): `predict.py::predict()` (XGBoost, Low/Medium/High, 88.4% test acc, returns `None`
  only if all 9 features missing), `clinical_categories.py` (display-only), `config.py`
  (`FEATURE_COLS` order matters, `ACTIVE_MODEL_VERSION="v1"`), artifacts in `models/artifacts/v1/`.
  `train.py`/`evaluate.py` are dev-only (`ml-train` group). No rules-engine fallback exists.
- Dependency direction **`modules → core`** only (import-linter). `core` must not import
  `modules`: use `django.apps.apps.get_model("monitoring", "RiskAssessment")` / `importlib`
  for runtime lookups. Routes are mounted explicitly in `config/api_router.py`; the
  `ProgramSpec` registry exists but is unused by choice.
- Shared bases (`core/common/models.py`): `UUIDPrimaryKeyModel` (all PKs are UUID — API ids
  are UUID **strings**), `TimeStampedModel`, `AddressMixin`, `Deactivatable` (**records are
  never physically deleted**).
- `ModuleRegistry` has an `organization` FK (per-hospital activation) but gating/seeding is
  deliberately not built (one module, always on).

## Key rules (each has full reasoning in the reference)
- **Login is email + password only.** Never re-add phone/username/identifier login (built,
  proven broken — 1 of 8 phone formats worked — reverted 15 Sep 2026). Phone is still collected
  as contact info: **convert blank → `NULL`** (`User.phone` is unique; `""` twice = IntegrityError).
- **Staff are invited, never given a password by an admin** (passwordless account,
  `requires_password_reset=True`, emailed one-time link). Keeps alert acknowledgement provable.
- **Region is derived, never asked**: `core/common/regions.py::region_for_country()`; do not add
  a region field. Unsupported country → `None`. Mapping is duplicated in the frontend
  (`hospital-onboarding/regions.ts`) — edit both; a test checks they agree.
- **Gestational age has one home**: `core/common/obstetrics.py::calculate_gestational_age()`,
  derived from EDD on read, never stored, never recomputed elsewhere. `gestational_age_long_display`
  (4-week "months") is additive, display-only.
- **Care team (`provider`/`nurse`/`care_manager`) lives on `Pregnancy`, not `Patient`** —
  pregnancy is a bounded episode; alert-escalation accountability must not be rewritten
  retroactively. Patient list embeds a flattened read-only copy.
- **`Pregnancy` is the enrol→discharge episode**; no separate enrolment table. `Patient.user`
  is optional. Self-registration = `PatientJoinRequest`.
- **Patient profile and join request** (7 Oct 2026, `docs/design/2026-10-07-patient-profile-and-join-request-design.md`):
  she fills in `GET/PATCH /api/my-profile/` once (identity only — never medical history, allergies
  or pregnancy dating; those are the clinician's). Name/phone/DOB/address live on `User`; only the
  rest is `PatientProfile` (no RLS: not tenant data). `POST /api/my-requests/` takes just
  `{organization, location?}`, refuses an incomplete profile, and freezes a server-built snapshot
  in `draft`. **There is no approve endpoint**: staff open `GET /api/patient-requests/{id}/` in the
  normal onboarding form and `POST /api/patients/` with `join_request` — that save is the
  approval (`onboard_from_join_request`, one transaction; sets `User.organization`). She lands in
  the requested branch, else the hospital default. Patients have **no gender field** (women only;
  `User.gender` stays for staff). The identity-document field is `national_id`, never `cnic`.
  Her care plan is **read-only** (`current-care-plan/` + `nutrition|exercise|medications|notes`,
  `/my-care-plans/`); she can never write to it, and **replies to notes were rejected on purpose**
  (nobody watches that channel — urgency goes through readings/alerts).
  Her readings/risk are read-only too: `pregnancies/{id}/my-readings|my-readings/latest|my-vitals-summary|my-risk`
  (patient serializers hide the device id and every staff review field; staff review filters are
  ignored for her). **A patient can never record a reading** (manual entry is hospital-side), and
  **the AI summary is staff-only**.
- **Risk scoring**: one producer (the model). `reassess_risk()` writes **one `RiskAssessment`
  per reading, unconditionally** (reversed 27 Sep 2026 — do not re-narrow to transitions-only),
  fires via `post_save` signal on `VitalReading`, then calls `sync_alert_for()` in the same
  transaction (idempotent). Postprocessing in Python: Africa+Medium → shown High
  (`final_risk_level`; `risk_level` keeps the raw answer); confidence below
  `Organization.effective_confidence_threshold` (default 0.800) → `flagged_for_review`.
- **No email for alerts or low-confidence flags** — deliberately deleted (shared Resend quota).
  Do not re-add without reading commit `da94b70`. In-portal alerts + `AlertEvent` audit remain.
- **Alert escalation** climbs only when `manage.py escalate_alerts` runs (cron, every minute);
  idempotent, tier computed from the clock. `AlertEvent` is append-only.
- **Risk review workflow**: `review_status` = pending/reviewed/escalated; `review`/`escalate`
  (label only, no Alert side effect)/`bulk-review` require `confirmed_risk_level`; gated
  `IsClinician | IsHospitalAdmin`. Needs `needs_attention` (actionable OR flagged) + pending.
  Alert acknowledge/resolve stays `IsClinician`-only. Patient deactivate/reactivate =
  `IsHospitalAdmin | IsCareManager`.
- **Workflows/Care Activities are query params on `GET /api/patients/`**, not standing
  endpoints (consolidated 26–27 Sep 2026 — do not recreate the queue endpoints):
  `?workflow=risk_review|low_confidence`, `?care_activity=monitoring_follow_up|unseen_readings|reading_reminder`,
  plus `?location=`/`?is_active=`/`?care_manager=`/`?provider=`/`?nurse=`/`?assigned_to=me`.
  Combined counts: `GET /api/patients/dashboard-kpis/`; org-wide totals (always org-scoped,
  never location-narrowed): `GET /api/patients/quick-lookup-kpis/`. Condition functions live in
  `modules/pregnancy/vitals/services.py` (resolved via `importlib`) and `core/analytics/services.py`.
  Care Activities are live stateless snapshots (no status/review). **Rejected on purpose**:
  Out of Range (needs the deleted rules engine), Priority Patients (CMS billing-cycle score).
- **Analytics**: `PatientAnalytics(patient, period_month, monitoring_seconds)`;
  `Patient.last_monitoring_contact_at`/`last_reading_at` are plain columns. Always a full
  recompute from source, never an incremental delta; signals live in the consumer app.
- **Clinical monitoring** (`core/monitoring`): `ClinicalTag`/`StatusLabel`/`NoteTemplate` are
  scoped org XOR location and copied down to new Locations via `post_save`; `MonitoringSession`/
  `MonitoringNote`/`PatientStatus` attach to `Patient` (+ optional `pregnancy`). Single
  programme — no RPM/CCM split, no billing.
- **Reading statistics / vitals summary / staff audit report**: plain arithmetic, no ML.
  BP & HR round to whole `int`; other vitals 1-decimal `float` (`round_metric_value()`).
  Audit report access reuses `can_manage_staff()`.
- **Admin must never allow** delete of organizations, patients, pregnancies, readings,
  assessments or alerts; readings/assessments are not editable (a correction is a new reading).
- **Clinical thresholds are unreviewed by an obstetrician** (model data, category cutoffs,
  escalation timings). Required before real clinical use — never imply otherwise.
- **Not built**: NGO emergency-response portal, `.claude/skills/momcare-*` content,
  `platform_admin` API.

## Conventions & testing
- Pagination envelope `{count, page, page_size, total_pages, next, previous, results}`
  (`DefaultPagination`); viewsets with `ordering_fields` use `StableOrderingFilter`.
- `test.py` never overrides `DATABASES` — tests run on Postgres like production. Mostly
  API-level integration tests, nothing mocked; `momcare_model`/`escalation.py`/`regions.py`
  are pure-function tests. `conftest.py` clears the throttle cache between tests.
- Security tests are validated by **fault injection** — when adding a protection, prove its
  test fails without it.
- `manage.py demo_setup [--reset-alerts]` — walkthrough data; refuses with `DEBUG` off; skips
  superusers/platform admins (never pass `--include-admins` unless asked).

## Deployment
Live: frontend `https://momcare.solutions` (Vercel, auto-deploys `main`), API
`https://api.momcare.solutions`. Read `DEPLOY.md` before touching anything deployment-related.

## Read the reference (`docs/CLAUDE-reference.md`) before changing…
| Area | Section in the reference |
|---|---|
| Any model/viewset tenancy | "Tenancy", "Scoping paths" |
| Moving apps between core/modules, app_label questions | "Devices, readings, risk and alerts live under modules/pregnancy/" |
| Patients/staff/monitoring/vitals/analytics serializers & endpoints | "Apps and what each owns" (big table) |
| Risk model, thresholds, per-reading storage, alerts | "The risk model…", "Risk scoring…", "One RiskAssessment row per reading", "Scoring and alerting are one transaction" |
| review/escalate/bulk-review, permission audit | "Risk review workflow" |
| Dashboard KPIs, Care Activities | "Care Activities" |
| Sign-in / phone handling | "Sign-in is email only" |
| Regions, gestational age, care-team placement | their own sections of the same names |
