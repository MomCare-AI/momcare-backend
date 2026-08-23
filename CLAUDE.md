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
column (directly, or via `Location.organization`), enforced two ways:

1. **Application-level**: the scoping mixins in `core/common/scoping.py`
   (`OrganizationScopedQuerysetMixin`, `LocationScopedQuerysetMixin`) — compose one of these
   into every viewset over tenant-owned data.
2. **Database-level**: Postgres Row-Level Security policies, added per-app via a migration
   once that app's schema is final. **Not yet implemented** — this is a known gap to close
   before any tenant data goes to production. Do not treat the application-level mixin alone
   as sufficient; it is the first layer of two, not the only layer.

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
  `staff`, `patients`.
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

The platform is built and running. Seven capabilities are complete, tested and pushed:

| # | Capability |
|---|---|
| 1 | Hospital registration with a review gate — pending / approved / rejected / suspended, evidence recorded |
| 2 | Six roles, enforced by DRF permission classes |
| 3 | Staff invitations — single-use links, revocation, 14-day expiry |
| 4 | Patients and pregnancy — consent history, obstetric dating, risk factors |
| 5 | Vitals and devices — ingestion, charts, simulator |
| 6 | Risk assessment — rules engine, attention queue |
| 7 | Alerts and escalation — three-tier ladder, email, append-only audit trail |

**202 backend tests, 19 frontend.** Security-critical tests validated by fault
injection: each protection was deliberately removed and the corresponding test
confirmed to fail.

`services.py` and `api/` are real implementations throughout, not stubs. Business
logic exists and is expected to.

### One structural divergence from the original design

The clinical work lives under **`core/`**, not `modules/`. `momcare_platform/modules/`
is still empty and the program-registry machinery in `core/common/programs.py` is
unused. That was a deliberate call: with one product and one team, a registry that
mounts routes without naming them added indirection and bought nothing. The
import-linter contract still holds — nothing imports `modules`.

Do not "restore" the modules layout without a concrete reason.

### What genuinely does not exist yet

- **Postgres RLS policies.** Still the known gap described under Tenancy above. The
  application-level scoping mixin is the only enforcement layer.
- **The NGO emergency-response portal.**
- **Any feature module under `modules/`.**
- **`.claude/skills/momcare-*`** — folders exist, content does not.

### Deployment

Not yet live. Read `DEPLOY.md` before touching anything deployment-related — every
environment variable, and which failures are silent. `../docs/deployment-plan.md`
carries the audit findings and the current blocker.


## Conventions & gotchas
- API ids are **UUID strings**; never assume integer ids.
- `test.py` never overrides `DATABASES` — tests run on the same Postgres engine as production.
- Pagination envelope: `{count, page, page_size, total_pages, next, previous, results}`
  (`core/common/pagination.py`, `DefaultPagination`). Any viewset with `ordering_fields` must
  use `StableOrderingFilter` from the same module.

---

## The clinical modules (added Aug 2026)

Read `../docs/PLAN.md` first — current status, decisions not to revisit, known gaps.

### Apps and what each owns

| App | Models | The thing to know |
|---|---|---|
| `patients` | `Patient` `Pregnancy` `PregnancyRiskFactors` `Consent` | `Patient.user` is **optional** (`SET_NULL`) — a rural patient may have no email and must still have a record |
| `monitoring` | `Device` `VitalReading` `RiskAssessment` | Readings attach to a **pregnancy**, not a patient — a heart rate of 110 means different things at 12 and 38 weeks |
| `alerts` | `Alert` `AlertEvent` | The push side. `AlertEvent` is append-only: escalation not written down is escalation that never happened |

### Two pure-function policy modules

Both are framework-free and database-free, so they test in milliseconds and the
clinical logic is readable in one place.

- `core/monitoring/risk_rules.py` — the obstetric thresholds. `ENGINE_VERSION` is
  recorded on every assessment.
- `core/alerts/escalation.py` — the tier ladder and its deadlines.

Neither has been reviewed by a practising obstetrician. That is stated in the docs
as a requirement before real use — do not quietly imply otherwise.

### Gestational age has exactly one home

`core/common/obstetrics.py::calculate_gestational_age()`. Derived from EDD on every
read, **never stored** — a stored column is wrong the next day. Never recompute it
anywhere else, or the list, the chart and the risk engine will disagree about how
pregnant someone is.

### Scoping paths

```
Patient    → location__organization          (never user__organization)
Pregnancy  → patient__location__organization
Reading    → pregnancy__patient__location__organization
Alert      → pregnancy__patient__location__organization
Staff      → user__organization
Device     → organization
```

Scope **before** lookup, so another tenant's row resolves to nothing. Cross-tenant
reads return **404, never 403** — a 403 confirms the record exists elsewhere.

### The AI seam

`RiskAssessment.source` is `"rules"` today and `"model"` when Ahmed's model lands —
same table, same endpoint, two producers. The rules engine leaves `score` and
`confidence` **null** rather than inventing numbers. Do not collapse this.

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
uv run pytest momcare_platform/core -q      # 202 tests, ~12s
```

Mostly **API-level integration tests** — a real request through routing, middleware,
JWT, permissions, serializer and a real Postgres. Nothing mocked. The two policy
modules are tested as pure functions.

Security tests were validated by **fault injection** — deliberately removing each
protection and confirming the tests failed. If you add a protection, prove its test
fails without it.

`conftest.py` clears the throttle cache between tests. Without it the suite shares
one `100/day` anon bucket and starts returning 429 once enough tests have logged in.

### Demo helper

`manage.py demo_setup [--reset-alerts]` — known passwords for a walkthrough.
Refuses to run with `DEBUG` off. **Skips superusers and platform admins by default**;
never pass `--include-admins` unless the user asks for exactly that.
