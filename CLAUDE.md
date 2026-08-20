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

This is a freshly scaffolded **foundation only** — infrastructure and structure, deliberately
stopped short of application/business logic until that's explicitly asked for. It is verified
working end-to-end against a real PostgreSQL database: `uv sync`, `manage.py check`,
`makemigrations`, `migrate`, and `createsuperuser` have all actually been run, not just
reviewed. What exists:

- Full directory structure matching the design in `docs/design/`.
- Working `config/settings/*.py`, `celery_app.py`, `urls.py` — Celery/Redis, JWT auth,
  CORS/CSRF all wired and confirmed working (eager-mode Celery in local dev, verified).
- Real, working implementations of every `core/common/*.py` infrastructure file (permissions,
  scoping, gating, audit middleware, health check, pagination, exception handler, JSON logging).
- Real model definitions for `User`, `Role`, `Organization`, `ModuleRegistry`, `AuditLog`,
  `Location`, `Staff`, `Patient`, with migrations generated and applied.
- `createsuperuser` bootstraps a `platform_admin` (no `organization`) — verified against a
  real database.
- CI (`ci.yml`), pre-commit config, and all quality-gate tooling (`ruff`, `mypy`,
  `import-linter`) — all confirmed clean/passing.

What does **not** exist yet (all deliberately deferred, not oversights):

- Any application/business logic — `services.py`, `signals.py`, and `tests/factories.py` are
  stubs across every app. This includes things like staff auto-creation and patient
  onboarding — do not add these until explicitly asked, even if a future task seems to need
  them; ask first.
- `api/serializers.py` / `api/views.py` for every app — currently stubs.
- Postgres RLS policies (see Tenancy section above) — the application-level scoping mixin is
  the only enforcement layer right now.
- Any feature module under `modules/`.
- The `.claude/skills/momcare-*` skill files — folders exist, content does not yet.

## Working-copy notes (2026-08-15)

- No `.env` exists in this checkout yet, so nothing has actually connected to Postgres from
  here — the "verified against a real database" claim above describes an earlier session's
  environment, not necessarily this checkout's current state. Follow the `Commands` setup
  steps (`.env` + `migrate`) before trusting `manage.py check`/`runserver` to work.
- `users/migrations/0001_initial.py` and `0002_seed_roles.py` were accidentally deleted and
  then restored on 2026-08-14. The restored `users` migrations are split differently than
  before: `0001_initial.py` (Role/User schema) → `0002_initial.py` (adds the `organization`
  FK / `locations` M2M — mirrors how `organization`/`locations` already split their own
  cross-app FKs to avoid a circular migration dependency) → `0003_seed_roles.py` (role-seed
  data migration, rewritten from scratch, not a byte-exact restore of the original file,
  though it seeds the same six roles).
- This project folder is manually mirrored (not live-synced) between
  `C:\Users\AHMED.PC\OneDrive\Desktop\FINAL YEAR PROJECT\Momcare_Backend` and
  `E:\FINAL YEAR PROJECT\Momcare_Backend`. Confirm which copy you're actually editing —
  changes to one do not automatically appear in the other.

## Conventions & gotchas
- API ids are **UUID strings**; never assume integer ids.
- `test.py` never overrides `DATABASES` — tests run on the same Postgres engine as production.
- Pagination envelope: `{count, page, page_size, total_pages, next, previous, results}`
  (`core/common/pagination.py`, `DefaultPagination`). Any viewset with `ordering_fields` must
  use `StableOrderingFilter` from the same module.
