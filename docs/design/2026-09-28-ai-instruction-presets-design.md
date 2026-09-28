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
organization id matches only that hospital's own rows. One transaction, so a concurrent
activation call can't leave two rows active at once — same "guard in the service layer, not a
DB constraint" call already made for `AIProviderConfig`'s own singleton fix, for the identical
reason: a partial unique index over a nullable column needs a sentinel-value workaround to treat
every `NULL` as "the same platform row," which is a real but disproportionate amount of migration
complexity for a guard a transaction already gives us correctly.

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

Organization-tier rows (`organization` set) are tenant-owned data — same scoping path as
`AISummary`: `AIInstructionPreset → organization` (direct column here, not a join, since this
table isn't per-patient). Needs an RLS migration matching
`core/organization/migrations/0024_note_template_row_level_security.py`'s own pattern:
tenant-owning role sees only its organization's rows, fail-closed, `platform_admin`/superuser
untouched by policy (already bypasses via role, not policy).

Platform-tier rows (`organization IS NULL`) are cross-tenant by nature, same footing as
`AIProviderConfig` itself (which has no RLS applied at all — there's no tenant to scope it to).
The RLS policy's own `USING` clause naturally handles this: a hospital's session variable can
never match `organization_id IS NULL`, so platform rows are invisible to every hospital-scoped
query without needing a special case in the policy — only the platform-admin endpoints (which
already run as `platform_admin`, exempt from RLS by role) ever see them, and even there the
queryset itself still filters `organization__isnull=True` so a platform admin's own list doesn't
also show every hospital's presets mixed in.

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
