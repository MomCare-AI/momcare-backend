# AI Summary — Design

**Date:** 2026-09-27
**Status:** Approved for implementation.

## Context

First piece of a larger AI initiative (AI Summary now; chatbot, nutrition plans, and exercise
plans are separate, later design passes — not in scope here). Provider is **OpenRouter**
(user-supplied API key), a single OpenAI-compatible gateway to many underlying models, chosen for
cost flexibility rather than a specific model's capabilities.

**Inspired by, not ported from**, a competitor platform's "AI Summary" card seen on a live
screenshot (`dev-platform.neurooceantours.com`) — regenerated narrative text over a patient's
vitals/notes/care team, with a "Generated at ..." timestamp. That platform's actual implementation
is not visible anywhere in the `Neuro_RPM` reference repo (checked: no AI/LLM dependency, no
matching endpoint, on `main`, `development`, or `client-uat`) — it must live in a separate
frontend/BFF codebase this project doesn't have access to. Nothing here is adapted from their code;
only the *feature idea* is a reference point. A second screenshot (a sparsely-populated,
newly-enrolled patient) confirmed the summary is genuinely LLM-composed from structured data, not a
fill-in-the-blank template, and that it handles missing data gracefully by stating gaps explicitly
("no known allergies," "no assigned care team," "no readings this month") rather than omitting them.

**Deliberately MomCare-specific, not a generic AI framework.** One provider (OpenRouter), one
client wrapper, hardcoded to this platform's own data shapes. No multi-provider registry, no
pluggable prompt system, no config surface built for a hypothetical second product. The one
deliberate exception is the admin-editable model/instructions config described below — an explicit
requirement, not scope creep.

**Scope is strictly per-patient, never per-organization.** One call, one patient, using only that
patient's own data — matching what both reference screenshots actually show (neither summary ever
references another patient on the roster).

## New app: `core/ai` (`momcare_platform/core/ai`, `app_label = "ai"`)

Aggregates data owned by several other apps (`Patient`, `Pregnancy`, `VitalReading`,
`RiskAssessment`, `MonitoringNote`/`MonitoringSession`), so it gets its own home — same reasoning as
`core/analytics`. Reaches `modules/pregnancy` data the same way `core/patients` already does —
`apps.get_model(...)` at runtime, never a static import, so `core must not import modules` stays
clean. **Already scaffolded** (`apps.py`, empty `models.py`, registered in `LOCAL_APPS`, verified
clean via `manage.py check`).

## Platform-wide AI configuration — `AIProviderConfig`

Single-row config, in `core/ai/models.py`:

| Field | Notes |
|---|---|
| `current_model` | `CharField` — the OpenRouter model id. **Validated against OpenRouter's own live model catalog on every write** (see below) — a typo can never be saved, because the value is checked against what OpenRouter actually serves at write time, not merely typed freely. |
| `max_words` | `IntegerField`, default `150` — the hard word cap. Platform-only; no per-organization override (model choice and word limit are cost/infra levers, not clinical customization). |
| `custom_instructions` | `TextField`, blank-allowed — free text the platform admin sets, appended after the fixed base prompt on every call. Literal text, not a structured keyword system — a single phrase or a full sentence both work, since the model just reads it as instructions. |

A `get_ai_config()` helper (`get_or_create` against a fixed row) is the single read path.

**Model catalog validation**: `openrouter_client.list_available_models()` calls OpenRouter's public
model-listing endpoint and returns valid ids (plus pricing/context metadata for display). The
platform-admin write endpoint (`PATCH /api/platform-admin/ai-config/`) checks the submitted
`current_model` against this list before saving, rejecting anything not currently served. A
separate `GET /api/platform-admin/ai-config/available-models/` exposes the catalog so the
platform-admin frontend can render it as a picker rather than a free-text box. If the catalog is
briefly unreachable, the endpoint falls back to whatever was fetched most recently rather than
blocking the page.

**Two-layer prompt (base layer fixed, everything else additive).** The base prompt (fixed, in
code, never admin-editable) carries the non-negotiables: use only the data provided, never
fabricate, stay within `max_words`, state gaps explicitly rather than omitting them, end with one
recommendation grounded in the data just described (or, for a deactivated patient, end by stating
the deactivation instead — see the Service section), plain prose. `custom_instructions` is appended
after that.

**Where the admin edits this**: a new endpoint under `core/platform_admin` — the first real
capability that app gets (previously a documented empty skeleton, "don't build without being
asked" — this is that ask, made directly). Gated to `ROLE_PLATFORM_ADMIN` only.

## Organization-level AI configuration — `Organization.ai_custom_instructions`

Each hospital's own `hospital_admin` can add free-text steering instructions on top of the
platform-wide ones — the same real, already-proven pattern this codebase uses for `ClinicalTag`,
`StatusLabel`, and `NoteTemplate` (hospital admins already fully customize those today).

- New field: `Organization.ai_custom_instructions` (`TextField`, blank-allowed) — a plain column on
  the existing `Organization` model, matching the precedent of `Organization.confidence_threshold`/
  `effective_confidence_threshold` (a per-hospital override with a platform-wide fallback).
- Editable by `hospital_admin` through the existing organization-settings endpoint.
- Model choice and `max_words` stay platform-only — see above.
- **Composition order, purely additive**: fixed base rules (code) → platform
  `AIProviderConfig.custom_instructions` (if set) → `Organization.ai_custom_instructions` (if set) →
  the patient's actual data.

**Location-level tier: explicitly deferred, not built this pass.** No hospital has asked for
per-branch (as opposed to per-hospital) customization yet, and the "org must approve this location
first" mechanism discussed has no precedent anywhere in this codebase. If ever added: no approval
gate needed on safety grounds — copy the existing `ClinicalTag`-style org/location scoping shape
directly.

## Model — `AISummary`

| Field | Notes |
|---|---|
| `patient` | `OneToOneField` to `Patient` — one live cached summary per patient, overwritten on refresh. |
| `content` | `TextField`, the generated narrative |
| `generated_at` | `DateTimeField` |
| `model_used` | `CharField` — snapshot of `AIProviderConfig.current_model` at generation time |
| `risk_level_at_generation` | `CharField` — the pregnancy's `final_risk_level` at the moment this summary was generated. Read on every new `RiskAssessment` write to detect whether the risk level has actually changed since — see Triggers below. |

**Scoping**: `AISummary → patient__organization` (same shape as `MonitoringNote`/`PatientAnalytics`).
**RLS policy is in scope for this work** — same migration shape as
`core/organization/migrations/0025_analytics_row_level_security.py`. `AIProviderConfig` is
platform-wide, not tenant data — no RLS policy needed for that table.

## OpenRouter client — `core/ai/openrouter_client.py`

- `generate(prompt: str, *, model: str, max_tokens: int) -> str | None` — the summary-generation
  call. `model` is always passed in explicitly (read from `AIProviderConfig`), never hardcoded —
  zero model-specific branching, which is what makes switching models a zero-logic-change
  operation.
- `list_available_models() -> list[dict] | None` — the catalog-validation call described above.

Uses `httpx` (new dependency — first outbound third-party HTTP call in this project; email goes
through Django's SMTP backend, not a REST call). **Best-effort, matching `core/common/mail.py`'s
existing convention**: `generate()` never raises into the caller, logs and returns `None` on any
failure. A clinician seeing yesterday's cached summary beats seeing an error page.

## Service — `core/ai/services.py::generate_patient_summary(patient) -> AISummary`

Gathers the full per-patient data slice, builds the layered prompt, calls the client, upserts the
`AISummary` row. **Full data scope**, all real fields this platform already computes:

- Pregnancy status — `gestational_age_long_display`
- Current risk — latest `RiskAssessment.final_risk_level` + `confidence`
- Monthly risk breakdown — `risk_this_month` (Low/Medium/High % this calendar month)
- Vitals — latest raw reading *and* the 30-day rolling average per metric
- Care team — provider/nurse/care manager names, or the explicit gap if a role is unassigned
- Most recent `MonitoringNote` — content + author
- Monitoring activity — `last_monitoring_contact_display`, `monitoring_time_display`
- Active `PatientStatus` entries
- Outstanding items — `pending_risk_count`/`needs_risk_review`, and a live `Alert` if one exists

**No PHI stripping — full patient data sent as-is, including name.** Explicit decision, made
deliberately after discussing the tradeoff (data leaves the platform via OpenRouter to whichever
model is selected) — not a default.

**Word cap: 150 words, hard.** Enforced two ways — a soft instruction in the base prompt and a
`max_tokens` ceiling on the OpenRouter request as a backstop.

**Closing-line fork**: if the patient is active, the base prompt ends with one recommendation
grounded in the data just described. If this generation was triggered by deactivation (see
Triggers), the prompt instructs the model to close by stating the deactivation instead of a
forward-looking recommendation — recommending future monitoring for someone no longer being
monitored doesn't make sense.

## Triggers — no manual regenerate button

Regeneration happens only through the system's own controlled triggers — deliberately no
user-facing "regenerate now" button, since that would be an uncontrolled path to a paid API call
with no natural rate limit. Four triggers:

1. **Enrollment** — a signal on patient creation generates the first summary immediately, rather
   than waiting for a patient to be picked up by the periodic cycle. Same pattern this codebase
   already uses elsewhere (a new `Location` auto-copies its hospital's tags on creation). Produces
   exactly the sparse-data-handled-gracefully output the second reference screenshot showed.
2. **Risk-level change** — every time a new `RiskAssessment` is saved, compare its
   `final_risk_level` to the patient's current `AISummary.risk_level_at_generation`. If different,
   mark the summary stale / trigger a regeneration. Most routine readings don't change the risk
   level, so this stays cheap — it only fires when something clinically real actually happened.
3. **Periodic safety net** — `manage.py refresh_ai_summaries`, same operational pattern as the
   existing `escalate_alerts` (cron / Task Scheduler), on a shorter interval than "once a day" (a
   few hours). Catches everything that doesn't trip the risk-level trigger but still accumulates
   (new notes, care-team changes, monitoring activity). **Excludes deactivated patients**
   (`is_active=True` filter) — no point refreshing summaries for inactive records.
4. **Deactivation** — a one-time signal fires the moment a patient is deactivated, generating one
   final summary that explicitly states the transition (e.g. "active in the program until
   [date]... now deactivated"), using the closing-line fork above. After that single write, the
   summary freezes for good — no further triggers touch it while the patient stays inactive. If
   later reactivated, the patient simply re-enters the normal active pool for triggers 2 and 3.

**Concurrency**: with manual triggering removed, the risk of two triggers firing on the same
patient at once is small (only the cron and the risk-change signal could theoretically overlap) —
worth a lightweight guard (e.g. `select_for_update()` around the upsert) but not a major mechanism,
given how rare the overlap actually is now.

## API

- `GET /api/patients/{id}/ai-summary/` — read the cached row (404 if the enrollment trigger hasn't
  run yet, which should be rare given it fires on creation).
- `GET /api/platform-admin/ai-config/` / `PATCH /api/platform-admin/ai-config/` — read/edit
  `current_model` (validated against the live catalog), `max_words`, `custom_instructions`.
  `ROLE_PLATFORM_ADMIN`-only.
- `GET /api/platform-admin/ai-config/available-models/` — the live OpenRouter catalog, for the
  picker UI.

The patient-facing `GET` is `IsHospitalStaff`-gated, same tier as reading the rest of a patient's
detail.

## Settings / new dependency

- `httpx` added to `pyproject.toml` (first HTTP client dependency in the project).
- `OPENROUTER_API_KEY` — env-backed, like every other credential in `settings/base.py`. Not
  needed for writing or testing code — only for the first live check and for production.
- `MOMCARE_AI_SUMMARY_DEFAULT_MODEL` / `MOMCARE_AI_SUMMARY_DEFAULT_MAX_WORDS` — seed values used
  only the first time `AIProviderConfig`'s row is created.
- `MOMCARE_AI_SUMMARY_REFRESH_HOURS` — the periodic safety net's staleness threshold.

**No cost/usage-tracking system built in MomCare** — OpenRouter's own dashboard already shows spend
per model; duplicating that would be redundant. `AISummary.model_used` gives per-record
traceability, which is enough.

## Testing

The OpenRouter client is mocked in every test — same posture as Resend never being hit for real in
mail tests; never spends real tokens, never depends on network access. Covers: each of the four
triggers fires generation under the right condition and not otherwise; `custom_instructions` from
both tiers appear in the built prompt in the right order; the closing-line fork produces the
deactivation-style ending when triggered by deactivation; the platform-admin config endpoint
rejects a `current_model` value not in the live catalog and is unreachable to non-platform-admin
roles; the periodic refresh command skips deactivated patients.

## Out of scope (this pass)

- The reference screenshot's "click an underlined value to see its source" citation-linking
  behavior — plain generated text only for v1.
- Chatbot, nutrition plans, exercise plans — separate design passes.
- Location-level AI instruction tier — deferred, documented extension point above.
- An org-wide/roster-level AI summary — this feature is strictly per-patient.
