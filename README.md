# MomCare Backend

A modular, multi-tenant backend for a maternal-health B2B platform. Hospitals register on
the platform and independently onboard their own staff and patients — many hospitals share
one deployment, each hospital's data kept isolated from every other's.

Backend-only, API-only: Django + Django REST Framework, JWT auth, PostgreSQL, Celery/Redis
for background work. There is no server-rendered UI — only the Django admin and the
auto-generated Swagger docs. A separate frontend SPA is the actual user-facing app, talking
to this service over `/api/`.

## Current status

**Live in production**, not just foundation. The section below described a freshly
scaffolded foundation with no clinical features — that was accurate in mid-August and is
now wrong in every particular. See `CLAUDE.md`'s "Status of this codebase" section for the
authoritative, actively-maintained version of this; the summary here is kept short
deliberately so there's only one place this can go stale from now on.

Seven capabilities are complete and tested end to end: hospital registration with a review
gate, six roles, staff invitations, patients and pregnancy tracking, vitals and device
ingestion, a rules-based risk engine, and alerts with a three-tier escalation ladder.
**347 backend tests, 30 frontend.** Security-critical protections (tenant scoping, RLS,
login gating) were validated by fault injection — each one deliberately removed and the
corresponding test confirmed to fail.

```bash
uv run pytest momcare_platform/core -q
```

**Deployed and live:** `https://momcare.solutions` (frontend, Vercel) /
`https://api.momcare.solutions` (this backend, Railway), Postgres on Neon, transactional
email via Resend. See `DEPLOY.md` for every environment variable and which failures are
silent.

**Tenant isolation is now two real layers, not one.** Application-level scoping
(`core/common/scoping.py`) plus Postgres Row-Level Security — RLS was written and tested
earlier, and as of 1 Sep 2026 is actually enforced in production (`DATABASE_URL` connects
as a restricted, non-bypassing role). See `CLAUDE.md`'s Tenancy section for the full detail.

**What genuinely still doesn't exist:**

- The NGO emergency-response portal.
- Any feature module under `momcare_platform/modules/` — the clinical work lives under
  `core/` instead, a deliberate divergence from the original modular-monolith design (see
  `CLAUDE.md`).
- Clinical validation of the risk-scoring thresholds by a practising obstetrician — stated
  plainly as a requirement before real clinical use, not implied otherwise.
- The AI model path (`RiskAssessment.source == "model"`) — the seam exists, nothing
  populates it yet.

## Architecture at a glance

- **Multi-tenant, shared database**: every hospital is one `Organization` row (not a
  separate database or schema). Every tenant-owned table is scoped by `organization_id`,
  enforced by `core/common/scoping.py`.
- **Two-tier roles**: `platform_admin` operates across every hospital; every other role
  belongs to exactly one hospital.
- **Modular monolith, with one deliberate divergence from the original plan**: `core/`
  holds every app, foundational and clinical alike. `modules/` — meant to hold pluggable,
  self-registering feature programs — is still empty; with one product and one team, that
  indirection was judged to buy nothing. See `CLAUDE.md`'s "Status of this codebase" for the
  reasoning. Do not "restore" the `modules/` layout without a concrete reason.

Full reasoning behind the original design is in
[`docs/design/2026-08-13-foundation-architecture-design.md`](docs/design/2026-08-13-foundation-architecture-design.md)
(historical — read alongside `CLAUDE.md` for what actually happened since).
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
