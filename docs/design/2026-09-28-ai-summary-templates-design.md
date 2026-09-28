# AI Summary Templates — Design Note

**Status:** Agreed in chat 2026-09-28, implemented directly (user chose to skip the written-spec
round for this one — this note exists as the record of what was agreed, not as a review gate).

## What this is

Today, `_build_prompt()`'s section structure (which fields go under "Vitals & Risk" vs "Care Team
& Activity", and in what order) is hardcoded in `core/ai/services.py`. This makes it a stored,
swappable thing instead — a platform admin or hospital admin can define a different **arrangement**
of the same fixed data fields, save it, switch to it, switch back — without a code deploy.

**What varies between templates:** the order and grouping of sections.

**What never varies, no matter which template is active:**
- The fixed set of ~17 data fields `_build_data_snapshot()` produces — no template can reference a
  field that doesn't exist, and no template can invent a new one.
- The core safety rules: never invent a value, state a gap plainly instead of omitting it, respect
  the word cap, close with exactly one recommendation grounded in the real data. These live in
  `_BASE_PROMPT`'s fixed preamble, outside anything a template edits.

This is the same "structure is data, safety rules are code" split already used for
`AIInstructionPreset` (extra appended text is data; the base prompt is code) — extended to also
let the *arrangement* of the base prompt's own data be data, while keeping the rules themselves
out of reach.

## Data model

`AISummaryTemplate` (`core/ai/models.py`), same shape and lifecycle as `AIInstructionPreset`:

- `organization` — nullable FK, `NULL` = platform tier, set = one hospital's own template.
- `name` — a label, e.g. "RPM Style", "Risk First".
- `sections` — `JSONField`, an ordered list of `{"label": str, "fields": [str, ...]}`. Each
  `fields` entry must be one of the fixed vocabulary field names (validated on write — an unknown
  field name is a 400, not a silently-ignored typo).
- `is_active`, `activated_at`, `created_by`, `created_at`/`updated_at` — identical semantics to
  `AIInstructionPreset`: immutable once created, never deleted, at most one active per scope,
  enforced via the same `pg_advisory_xact_lock` pattern.

## Fixed field vocabulary

Exactly the keys `_build_data_snapshot()` already produces — `patient_name`, `gestational_age`,
`current_risk_level`, `risk_this_month`, `latest_readings`, `thirty_day_average`,
`provider_name`, `nurse_name`, `care_manager_name`, `recent_note`, `recent_note_author`,
`last_monitoring_contact_display`, `last_reading_display`, `monitoring_time_display`,
`active_statuses`, `pending_risk_count`, `has_open_alert`. A single source list in
`core/ai/services.py` both validates template writes and drives the fallback default below.

## Precedence and fallback

`_build_prompt()` picks the active template in this order:

1. The patient's own hospital's active template, if one exists.
2. Otherwise the platform's active template, if one exists.
3. Otherwise a **built-in default** — the exact two-section grouping already shipped (Vitals &
   Risk, then Care Team & Activity) — so the system always has a working structure even before
   anyone creates a stored template. This is not a database row; it's the same Python fallback
   that exists today, kept as-is.

Unlike instruction presets (platform + org text both get appended together), only one template
structure can be "the" structure for a given summary — org overrides platform, platform overrides
the built-in default, never a merge of more than one.

## Endpoints

Same shape as the instruction-preset endpoints, mirrored at both tiers:

- `GET/POST /api/platform-admin/ai-config/summary-templates/`
- `POST /api/platform-admin/ai-config/summary-templates/{id}/activate/`
- `POST /api/platform-admin/ai-config/summary-templates/{id}/deactivate/`
- `GET/POST /api/organization/me/summary-templates/`
- `POST /api/organization/me/summary-templates/{id}/activate/`
- `POST /api/organization/me/summary-templates/{id}/deactivate/`

## Out of scope (per the chat)

- Anything about *how* a section is phrased/worded — only *which* fixed fields go *where*.
- Free-text template bodies — sections are structured JSON, not a prompt string, specifically so
  the safety rules can't be edited away by editing a template.
