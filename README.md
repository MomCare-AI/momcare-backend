# MomCare Backend

A modular, multi-tenant backend for a maternal-health B2B platform. Hospitals register on
the platform and independently onboard their own staff and patients — many hospitals share
one deployment, each hospital's data kept isolated from every other's.

Backend-only, API-only: Django + Django REST Framework, JWT auth, PostgreSQL, Celery/Redis
for background work. There is no server-rendered UI — only the Django admin and the
auto-generated Swagger docs. A separate frontend SPA is the actual user-facing app, talking
to this service over `/api/`.

## Current status

The foundation plus the **access-control layer** — hospital onboarding and staff
management work end to end. There are no maternal-health features yet.

**Access model.** Tenant membership is granted from *inside* a tenant, never claimed from
outside it:

- **Hospitals** apply at `/api/auth/register/`. Registration issues **no token**:
  `Organization.status` starts `pending` and `LoginView` refuses sign-in until a platform
  admin approves it. No public API verifies Pakistani facility licences — PMDC registers
  practitioners, while facilities are licensed provincially (PHC, SHCC, KP HCC, Balochistan
  HCC, IHRA) — so approval is a human check with recorded evidence: issuing authority,
  licence document, reviewer, timestamp, and a note of what was verified. The applicant is
  emailed on submission and again on the decision.
- **Clinical staff** are invited by their hospital admin and set their own password via a
  single-use link. Organization and role are read from the invite row on acceptance, never
  from the request, so nobody can join another hospital or claim a higher role.
- **Patients** are enrolled clinically — not yet implemented.

**Testing.** 23 API-level integration tests cover authentication, authorization and tenant
isolation, validated by fault injection: removing the tenant filter fails the isolation
test, and disabling the login gate fails four others.

```bash
uv run pytest momcare_platform/core -q
```

**Implemented and verified working:**

- Hospital registration, review gate, and admin review actions (approve / reject / suspend);
  tenants are never hard-deleted.
- Staff invitations with expiry, revocation, and replay protection.
- `/api/organization/me/` and the staff endpoints, scoped via `OrganizationScopedQuerysetMixin`.
- Best-effort transactional email (`core/common/mail.py`) — a mail outage never fails a
  registration or rolls back an approval.

- Full project structure — settings split (`local`/`test`/`production`), Celery + Redis,
  JWT auth config, CORS/CSRF for a cookie-based refresh token.
- Six foundational apps: `users` (custom `User` + `Role`), `organization` (the hospital
  record + audit log + per-hospital module activation), `locations`, `staff`, `patients`
  (bare entity — no medical fields yet), and `common` (shared infrastructure: permissions,
  tenant scoping, audit middleware, health check, JSON logging, pagination).
- Real database migrations, generated and applied against a live PostgreSQL database.
- Superuser bootstrap (`createsuperuser`) — automatically creates a platform-level admin,
  not scoped to any single hospital.
- Six seeded roles: `platform_admin`, `hospital_admin`, `provider`, `nurse`,
  `care_manager`, `patient`.
- Quality gates: `ruff`, `mypy`, `import-linter` (enforcing the `modules → core` dependency
  direction), pre-commit hooks, a CI workflow that runs lint + type-check + tests on every PR.

**Not implemented yet** (deliberately stopped here — foundation only, application logic is a
separate future step, see `docs/design/`):

- Any application/business logic — `services.py`, `signals.py`, and `tests/factories.py` are
  stubs across every app (e.g. no staff auto-creation, no patient onboarding logic yet).
- API endpoints beyond auth, organization and staff — `locations` and `patients` are still
  stubs, and `patients` has no medical fields.
- Postgres Row-Level Security policies (tenant isolation currently relies on the
  application-level scoping mixin alone — RLS is the planned second layer, not yet written).
- Any actual maternal-health feature (pregnancy tracking, vitals, care plans) — no feature
  module exists under `momcare_platform/modules/` yet. That's a separate, future design pass.
- Hosting/deployment — no cloud provider chosen yet; `deploy/` is an empty placeholder.

## Architecture at a glance

- **Multi-tenant, shared database**: every hospital is one `Organization` row (not a
  separate database or schema). Every tenant-owned table is scoped by `organization_id`,
  enforced by `core/common/scoping.py`.
- **Two-tier roles**: `platform_admin` operates across every hospital; every other role
  belongs to exactly one hospital.
- **Modular monolith**: `core/` holds foundational apps everything depends on; `modules/`
  will hold pluggable feature programs, each self-registering without `core` ever importing
  them (kept in one deployable unit, no microservices overhead).

Full reasoning behind every decision above is in
[`docs/design/2026-08-13-foundation-architecture-design.md`](docs/design/2026-08-13-foundation-architecture-design.md).
Day-to-day conventions (which base model class to use, how permissions are structured, etc.)
are in [`CLAUDE.md`](CLAUDE.md).

## Setup

Requires Python 3.14 and a running PostgreSQL server. Redis is optional locally — Celery
runs tasks synchronously in dev, no broker needed (see `config/settings/local.py`).

```bash
uv sync
cp .env.example .env          # fill in DATABASE_URL, DJANGO_SECRET_KEY, etc.
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run pre-commit install     # wires the pre-commit hooks into git — once per clone
uv run python manage.py runserver
```

Then visit `http://localhost:8000/health/` — it should return `{"status": "ok", "database": "ok"}`.

`pre-commit install` requires a git repository to already exist at this point (it wires into
`.git/hooks/`) — run `git init` or `git clone` your repo first if you haven't yet.

## Tests

```bash
uv run pytest
```

Runs against the same PostgreSQL engine as production (never SQLite) — see `CLAUDE.md` for why.

## Quality gates

```bash
uv run ruff check .          # lint
uv run ruff format .         # format
uv run mypy momcare_platform  # type check
uv run lint-imports          # verify the core/modules boundary hasn't been violated
uv run pre-commit run --all-files   # manually run every hook once, on demand
```

Once `pre-commit install` has been run (see Setup), most of the above run **automatically**
on every `git commit` — you don't have to run them by hand every time. The commands above are
for checking something before you attempt the commit, or debugging why one got blocked.

## Committing changes

```bash
git add <files>              # stage what you changed — avoid `git add -A`, it can catch secrets
git commit -m "your message" # pre-commit hooks run here automatically; blocked if any fail
git push                     # send it to the remote repo
```

If a hook modifies a file (e.g. `ruff` auto-fixing something), `git add` that file again and
re-run `git commit`.

## Changing a model

Editing a `models.py` file does nothing to the database by itself — two more steps are
always required, every time:

```bash
uv run python manage.py makemigrations   # generates the migration file describing the change
uv run python manage.py migrate          # actually applies it to the database
```
