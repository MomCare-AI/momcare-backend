# AI Instruction Presets — Design Spec

**Status:** Approved in chat 2026-09-28, pending written-spec review.

## Context

`AIProviderConfig.custom_instructions` (platform tier) and `Organization.ai_custom_instructions`
(organization tier) are today plain mutable text fields — see
`docs/design/2026-09-27-ai-summary-design.md`. Whatever a platform admin or hospital admin last
typed is the only copy; there is no record of what was set before, and switching back to an
earlier wording means retyping it from memory.

The ask: keep a history of instruction text at both tiers, let the owning admin **activate** a
past one, **deactivate** the current one, or **create a new one** — without losing anything that
was ever in production use.

## Model

One new model, `AIInstructionPreset`, in `core/ai/models.py`:

| Field | Notes |
|---|---|
| `organization` | `ForeignKey("organization.Organization", null=True, blank=True, on_delete=CASCADE, related_name="ai_instruction_presets")`. `NULL` = platform-tier preset; set = that hospital's own preset. Same nullable-FK-as-tier-marker idea `AISummary`'s siblings use, simpler than `ClinicalTag`/`StatusLabel`'s org-XOR-location pattern since there are only two tiers here, not three. |
| `name` | `CharField(max_length=200)` — required. Short label shown in the list (e.g. "Winter 2026 — emphasize hydration"). |
| `content` | `TextField()` — the instruction text itself, same free-text-appended-to-prompt meaning `custom_instructions` has today. |
| `is_active` | `BooleanField(default=False)`. At most one `True` per scope (per organization, and separately at most one platform-wide) — enforced in the service layer, not a DB constraint (see Activation below). |
| `activated_at` | `DateTimeField(null=True, blank=True)`. Set every time this preset is activated; left untouched on deactivation, so it always answers "when was this most recently made active" even after it's no longer the active one. A separate fact from `updated_at` (record last touched) even though today activation is the only thing that ever touches a preset after creation — naming it explicitly avoids that coincidence quietly becoming load-bearing. |
| `created_by` | `ForeignKey("users.User", null=True, on_delete=SET_NULL)` — who authored it, matching `NoteTemplate`'s own audit field. |
| `created_at` / `updated_at` | via `TimeStampedModel`, as everywhere else. |

Presets are **immutable after creation** (no PATCH endpoint) and **never deleted** (no DELETE
endpoint) — both per your answers in the design conversation. "Editing" is always create-new +
activate; "removing" is always deactivate. `updated_at` therefore only ever moves when
`is_active` flips.

Ordering: `-created_at` (newest first), matching every other catalog list in this codebase.

## Replacing the existing fields

`AIProviderConfig.custom_instructions` and `Organization.ai_custom_instructions` are **removed**,
not kept alongside the new model — a flat text field and a preset catalog would be two producers
of the same fact, exactly the kind of duplication this codebase avoids elsewhere (see
`RiskAssessment`'s "exactly one producer" precedent).

`core/ai/services.py::_build_prompt`'s two `if config.custom_instructions:` /
`if org_instructions:` lines become reads through the active preset instead:

```python
platform_instructions = (
    AIInstructionPreset.objects.filter(organization__isnull=True, is_active=True)
    .values_list("content", flat=True)
    .first()
    or ""
)
org_instructions = (
    AIInstructionPreset.objects.filter(organization=patient.organization, is_active=True)
    .values_list("content", flat=True)
    .first()
    or ""
)
```

No preset active for a scope behaves exactly like today's empty `custom_instructions` default —
that section of the prompt is simply omitted, no new fallback logic needed.

## Activation

A service function per tier (`core/ai/services.py`):

```python
def activate_instruction_preset(preset: AIInstructionPreset) -> None:
    with transaction.atomic():
        AIInstructionPreset.objects.filter(
            organization=preset.organization, is_active=True,
        ).exclude(pk=preset.pk).update(is_active=False)
        preset.is_active = True
        preset.activated_at = timezone.now()
        preset.save(update_fields=["is_active", "activated_at", "updated_at"])
```

Calling `activate` on a preset that is already active, or `deactivate` on one that is already
inactive, is a 400 — same "acting on a state that doesn't apply" convention the risk-review
`review`/`escalate` actions already use elsewhere in this codebase, rather than a silent no-op
that could mask a caller bug (e.g. a double-click racing itself).

`organization=preset.organization` naturally scopes the "deactivate the others" step correctly
for both tiers: `NULL` matches only other `NULL`-organization (platform) rows, a real
organization id matches only that hospital's own rows.

**Corrected 2026-09-28, after code review** — this section originally claimed "one transaction,
so a concurrent activation call can't leave two rows active at once." That's false:
`transaction.atomic()` gives atomicity, not serializability. Under READ COMMITTED, two concurrent
activations of two *different*, not-yet-active presets in the same scope can both commit, each
leaving its own row active — neither activation's "deactivate the others" `UPDATE` touches the
other's row (since neither is active yet), so there's no shared row for the two transactions to
contend over without an explicit lock. The actual guard is `pg_advisory_xact_lock`, keyed on the
scope (the org uuid, or a fixed sentinel string for the platform tier) and held for the duration
of the activating transaction — see `core/ai/services.py::_lock_instruction_preset_scope`. This
still avoids the partial-unique-index-over-a-nullable-column problem the original reasoning
correctly identified (the same tradeoff already made for `AIProviderConfig`'s own singleton fix):
an advisory lock needs no schema change and treats the platform tier's sentinel key the same way
a real org uuid is treated, with no NULL-handling special case.

A separate `deactivate_instruction_preset(preset)` just sets `is_active = False` unconditionally
(no other row needs touching).

## Endpoints

Mirrors the existing tier split exactly — two new files, no changes to the existing
`AIProviderConfigView` / `OrganizationAIInstructionsView` beyond removing the fields they no
longer serve.

**Platform tier** (`core/platform_admin/api/views.py`, `ROLE_PLATFORM_ADMIN` only, matching
`AIProviderConfigView`):
- `GET /api/platform-admin/ai-config/instruction-presets/` — list, `organization__isnull=True`,
  paginated (`DefaultPagination`).
- `POST /api/platform-admin/ai-config/instruction-presets/` — create (`organization` forced to
  `None` server-side, never accepted from the request body).
- `POST /api/platform-admin/ai-config/instruction-presets/{id}/activate/`
- `POST /api/platform-admin/ai-config/instruction-presets/{id}/deactivate/`

**Organization tier** (`core/organization/api/views.py`, `IsHospitalAdmin`, matching
`OrganizationAIInstructionsView`):
- `GET /api/organization/me/instruction-presets/` — list, `organization=request.user.organization`.
- `POST /api/organization/me/instruction-presets/` — create (`organization` forced to
  `request.user.organization` server-side).
- `POST /api/organization/me/instruction-presets/{id}/activate/`
- `POST /api/organization/me/instruction-presets/{id}/deactivate/`

Both `activate`/`deactivate` actions 404 (not 403) on a preset id from the other tier or another
organization — same "scope before lookup" rule as everywhere else in this codebase.

`AIInstructionPresetSerializer`: `id`, `name`, `content`, `is_active`, `created_at`,
`created_by` (read-only, `source="created_by.get_full_name"` display name, same pattern
`AuditLogSerializer` already uses for `user_name`). `name`/`content` required and writable on
create only — the view layer never routes a request at this serializer for update.

## Tenancy and RLS

**Corrected 2026-09-28, after code review — the original text below this line was wrong.**
There is no per-role RLS exemption anywhere in this project. Every request connects to Postgres
as the same single `momcare_app` role, `NOBYPASSRLS` (see `DEPLOY.md`'s "Database roles"
section) — `platform_admin` is a Django-level role check, invisible to Postgres, not a
Postgres-level one. Two things actually determine what a request can see:

1. **The RLS policy's own `USING`/`WITH CHECK` clauses.** Organization-tier rows (`organization`
   set) are tenant-owned — same scoping path as `AISummary`: `AIInstructionPreset → organization`
   (direct column, not a join, since this table isn't per-patient). Platform-tier rows
   (`organization IS NULL`) must be **readable by every session** — a hospital's own AI summaries
   need the platform-wide instructions to reach their prompt just as much as a platform admin's
   picker does — but **writable only by a session that already owns the scope it's writing**, so
   an ordinary hospital session can never create or repoint a platform-tier row. That needs the
   policy's `USING` and `WITH CHECK` to differ (a `FOR ALL` policy with only `USING` reuses that
   expression for both): `USING` allows a hospital's own rows OR any `organization_id IS NULL`
   row; `WITH CHECK` allows only a hospital's own rows. See
   `core/organization/migrations/0030_fix_ai_instruction_preset_platform_tier_visibility.py` —
   the migration that fixed this after review found the first version (0028) made platform-tier
   rows invisible to every ordinary hospital session, not just protected from being written by
   one; `generate_patient_summary`'s own read (an ordinary org-scoped session, not `bypass_rls()`)
   was silently getting zero platform instructions on every request-driven trigger.

2. **`TenantAwareJWTAuthentication`'s own bypass for a token with no `org_id` claim** (a platform
   admin's token). That code path calls `bypass_rls()`, and because `SET LOCAL app.rls_bypass`
   survives `RELEASE SAVEPOINT` (confirmed empirically during review), the flag stays `'on'` for
   the rest of that request's transaction — which is what actually lets
   `AIInstructionPresetListCreateView.post()` (platform tier) insert an `organization_id IS NULL`
   row: the corrected policy's `WITH CHECK` only allows a session's *own* org, so without this
   leak-through a platform admin's own writes would fail their own policy. This is incidental,
   not a designed-in per-role exemption — a future change tightening `bypass_rls()` to scope
   itself more precisely would silently break platform-tier preset creation with no other code
   change. `core/platform_admin/api/views.py`'s platform-tier views carry a comment naming this
   dependency explicitly, for exactly that reason.

The queryset in every list endpoint still filters `organization__isnull=True` /
`organization=org` explicitly regardless of what RLS alone would return — so a platform admin's
own list never shows every hospital's presets mixed in with the platform ones, and a hospital's
own list never shows the platform-tier presets alongside its own (even though RLS alone would let
a hospital's session *read* platform-tier rows, per point 1 above).

## Migration

One new model migration (`AIInstructionPreset`), a data migration copying any current non-empty
`AIProviderConfig.custom_instructions` into a new platform-tier preset (`name="Migrated
instructions"`, `is_active=True`) and any current non-empty
`Organization.ai_custom_instructions` per hospital into its own org-tier preset the same way, then
a schema migration dropping both old fields. Three migrations, in that order, so
`makemigrations --check` stays clean and nobody's current instructions silently vanish on
deploy.

## Out of scope

- Editing a preset's `name`/`content` after creation — explicitly rejected in the design
  conversation.
- Deleting a preset, ever — explicitly rejected.
- An `activated_by`/activation-history audit trail beyond `created_by` and `updated_at` — not
  asked for; `updated_at` already tells you *when* a preset was last activated or deactivated,
  just not *who* did it. Worth a follow-up if that gap matters later.
- Any UI/frontend work — backend only, matching the rest of this feature's scope.

## Testing

Mocked-throughout is not relevant here (no OpenRouter call in this subsystem at all — presets are
plain CRUD + one service function). Coverage needed: creating a preset at each tier (and that
`organization` can't be spoofed from the request body); activating deactivates exactly the
sibling scope's previous active row and no others (a platform activation must never touch an
organization's active row, and one hospital's activation must never touch another's); deactivating
with nothing else active leaves the prompt builder with empty instructions for that scope (an
integration test through `_build_prompt`, not just the preset table); 404-not-403 cross-tenant
and cross-tier; the two-step migration actually preserves non-empty existing instructions as an
active preset.
