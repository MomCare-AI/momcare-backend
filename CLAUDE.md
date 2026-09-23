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

**516 backend tests (4 skipped) as of the 23 Sep 2026 revision, 30 frontend.**
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
| `staff` | `Staff` `SecondaryProvider` | `SecondaryProvider` is an **external** clinician — no login, no role, never in `/api/staff/`. It is a table rather than columns on Patient because one referring doctor is shared across many patients. Unlike the reference platform's version it carries an `organization` FK: Neuro_RPM is single-tenant, MomCare is not, and without it every hospital would read every other hospital's referral list. |
| `patients` | `Patient` `Pregnancy` `PatientJoinRequest` | `Patient.user` is **optional** (`SET_NULL`) — a rural patient may have no email and must still have a record. The care team is three direct columns on `Pregnancy` — `provider` (the accountable lead, what alert escalation routes to), `nurse`, `care_manager` — one of each at a time. **`Pregnancy` is the enrol→discharge episode** — there is no separate programme-enrollment table, and a `PatientProgramEnrollment` was deliberately removed for duplicating that role (see `patients/migrations/0011`). MomCare runs one programme; if a second ever arrives, that table earns its place back then. The seven obstetric-history answers and `Patient.consent_date` are plain columns, not satellite tables — `PregnancyRiskFactors` and `Consent` were folded in by `patients/migrations/0012`: one was answered only per-pregnancy, the other became a single date matching the reference platform. Consent is therefore **no longer mandatory** at onboarding. `PatientJoinRequest` is the self-registration path (Part B, built 2026-09-15): a woman registers in the app with `User.organization = NULL`, browses approved hospitals, and sends a request carrying a `draft` of her own details. **No `Patient` row exists until a hospital approves** — and approval calls the same `onboard_patient()` a walk-in uses, so there is exactly one creation path. Her own two endpoints run inside `bypass_rls()` with a `user=request.user` filter, because an org-less token makes the fail-closed policy hide her own rows from her. `ClinicalNote` was also removed (`patients/migrations/0013`) — notes are monitoring, not onboarding, and will be designed fresh alongside the reference platform's tags/templates/sessions rather than half-existing as a text field. |
| `modules/pregnancy/vitals` | `Device` `VitalReading` `RiskAssessment` | Readings attach to a **pregnancy**, not a patient — a heart rate of 110 means different things at 12 and 38 weeks. Lives under `modules/`, not `core/`, since 16 Sep 2026 (see "Devices, readings, risk and alerts" above) — the Django `app_label` is still `monitoring`, unchanged from its former `core/` home, so table names and RLS policies didn't move with it. |
| `modules/pregnancy/alerts` | `Alert` `AlertEvent` | The push side. `AlertEvent` is append-only: escalation not written down is escalation that never happened. Same `core/`→`modules/` move, `app_label` still `alerts`. |
| `core/monitoring` | `ClinicalTag` `MonitoringSession` `MonitoringNote` | Built 23 Sep 2026 — clinical contact logging (calls, chart reviews, notes), adapted from the reference platform's own `core.monitoring` with three deliberate departures: no RPM/CCM program split (MomCare has exactly one programme, so `MonitoringSession` carries a single `duration_seconds`, not a per-billing-program breakdown); attaches to **`Patient`** with an optional **`pregnancy`** FK auto-filled from `patient.current_pregnancy` (not `Pregnancy` alone — `onboard_patient()` allows a patient with no pregnancy yet, unlike readings/alerts which always have one); `ClinicalTag` is tenant-scoped (`organization` XOR `location`, exactly one) where the reference platform's own tag list is global, because MomCare is multi-tenant and it isn't. A new `Location` auto-copies its hospital's org-level tags down as independent rows (`monitoring/signals.py`), the same `post_save` pattern the reference platform uses for `NoteTemplate`/`ChronicCondition`/`Medication`, applied here to a model that platform never scoped this way. **`app_label` is `clinical_notes`, not `monitoring`** — that label already belongs to `modules/pregnancy/vitals` (kept from its own former `core/` home), so this app, despite being the thing `core/monitoring`'s *name* was freed for, needed a different label. Endpoints are flat/patient-nested APIViews matching the rest of this project's convention, not the reference platform's `ModelViewSet`s. |

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

### Scoping paths

```
Patient       → location__organization          (never user__organization)
Pregnancy     → patient__location__organization
Reading       → pregnancy__patient__location__organization
Alert         → pregnancy__patient__location__organization
Staff         → user__organization
SecondaryProvider → organization            (direct column, like Device)
PatientJoinRequest → organization           (direct column; patient side uses bypass_rls + self-filter)
Device        → organization
OrganizationDeactivationRequest → organization    (direct column, like Device)
MonitoringSession → patient__organization     (Patient, not Pregnancy — see core/monitoring app row above)
MonitoringNote    → patient__organization
ClinicalTag       → organization XOR location__organization   (exactly one column set; see core/monitoring app row)
Notification      → organization            (direct column, like Device)
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

### Scoring and alerting are one transaction

`reassess_risk()` writes an assessment **only when the level changed**, then calls
`alerts.services.sync_alert_for()`. An assessment saying "critical" with no alert is
a state this system must not be able to reach.

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
uv run pytest momcare_platform/core -q      # 485 passed, 4 skipped, as of 23 Sep 2026
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
