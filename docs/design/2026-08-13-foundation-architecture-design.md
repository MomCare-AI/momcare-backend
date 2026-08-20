# MomCare Backend — Foundation Architecture Design

**Date:** 2026-08-13
**Status:** Approved, scaffolded. Feature-module design (maternal-health domain) is a
separate, future design doc — deliberately out of scope here.

## Context

MomCare is a B2B multi-tenant SaaS platform: hospitals register on the platform and onboard
their own staff and patients. This document records the foundational architecture decisions
made before any application code was written, using the Neuro_RPM reference project's
Universal Modular Monolith Blueprint as the starting template, adapted where MomCare's
business model diverges from it.

## Decisions

### 1. Tenancy: shared tables + `organization_id` + Postgres RLS

Considered three options: database-per-hospital (Neuro_RPM's own model), schema-per-hospital,
and shared tables with a tenant column.

**Chosen: shared tables.** Rationale — expected scale is dozens to hundreds of
self-serve-onboarding hospitals. Schema-per-tenant and database-per-tenant both impose
migration-replay and connection-pooling costs that compound with tenant count; shared tables
with `organization_id` is the standard, proven pattern at this scale (this is what most B2B
SaaS platforms run). Enforced two ways: an application-level scoping mixin
(`core/common/scoping.py`) and Postgres Row-Level Security as an independent second layer —
**RLS policies are not yet implemented**; that is open follow-up work, not a design gap.

Rejected schema-per-tenant specifically because Django has no first-party support for it,
`django-tenants`-style libraries replay every migration once per schema on every deploy, and
it conflicts with PgBouncer transaction-pooling (`search_path` is session state).

### 2. Two-tier roles

- `platform_admin` — MomCare's own operators, not scoped to a single hospital.
- `hospital_admin`, `provider`, `nurse`, `care_manager`, `patient` — each scoped to exactly
  one hospital via `User.organization`.

This is a genuine divergence from Neuro_RPM, whose roles are all implicitly "within the one
organization" since it has no multi-tenant concept.

### 3. Directory structure

Mirrors Neuro_RPM's real (not idealized) structure, verified against the actual codebase
file-by-file rather than assumed: `core/common`, `core/users`, `core/organization`,
`core/locations`, `core/staff`, `core/patients` as separate small apps (not one bundled
"organizational" app), each with the standard `admin.py`/`apps.py`/`models.py`/`services.py`/
`api/`/`tests/` internal shape, tests co-located per-app (never centralized).

Dropped from Neuro_RPM's structure: the unedited Sphinx docs scaffold (dead boilerplate),
`docs/pycharm/` (IDE-specific), `core/analytics`/`core/monitoring` (later additions, not
foundational), and any medical/clinical content.

### 4. Module activation is per-hospital

`ModuleRegistry` carries an `organization` FK, unlike Neuro_RPM's global version — each
hospital independently activates the feature modules it subscribes to. `core/common/gating.py`
reflects this: `ModuleGatedViewSet` resolves the requesting user's organization and checks
activation against it; platform admins bypass the gate entirely (module subscription is a
per-hospital concept, meaningless for a cross-hospital operator account).

### 5. No clinical/maternal-health content in the foundation

`core/patients/models.py` is deliberately bare (location, user, MRN, consent date — no
pregnancy, vitals, or care-plan fields). The first feature module's design is explicitly
future work, not part of this pass — matches the blueprint's guidance to decompose an
oversized project into independently-specced sub-projects rather than design everything at
once.

### 6. HIPAA / compliance pieces carried over from the proven Neuro_RPM pattern

`AuditLogMiddleware`, `Deactivatable` (soft-delete only), `UUIDPrimaryKeyModel`, and the
DRF exception handler are adopted essentially unchanged — these are compliance mechanisms
that don't depend on tenancy model or domain.

### 7. DevOps / hosting: explicitly deferred

Infrastructure (cloud provider, Terraform, load balancing, deployment pipeline) is out of
scope for this pass by deliberate choice — the application architecture doesn't depend on
where it eventually runs, and the decision was deferred until there's a working application
worth deploying. `deploy/` exists as an empty folder placeholder only.

## Verification

This foundation is not just reviewed — it was actually run. `uv sync`, `manage.py check`,
`makemigrations`, `migrate`, and `createsuperuser` were all executed against a real local
PostgreSQL database (confirmed `createsuperuser` bootstraps a `platform_admin` with no
`organization`, as designed). `ruff` and `import-linter` are both clean.

An initial pass also added working `services.py`/`signals.py` logic (staff auto-creation,
patient onboarding) plus fixtures and a passing test suite exercising it, to prove the
pattern end-to-end. That logic was deliberately reverted back to stubs afterward — this is a
**foundation-only** pass, and application/business logic is explicitly out of scope until
asked for separately (see CLAUDE.md's "Status of this codebase"). The pattern is proven to
work; it just isn't in the codebase yet.

## Known gaps (tracked, not accidental)

- No application/business logic yet — `services.py`/`signals.py`/`tests/factories.py` are
  stubs across every app, by deliberate scope decision (see Verification above).
- Row-Level Security policies not yet written (see Decision 1).
- `api/serializers.py` / `api/views.py` are stubs across every app.
- `.claude/skills/momcare-*` folders exist; skill content does not yet.
- Cloud/hosting provider not yet chosen (deliberately deferred, see Decision 7).
