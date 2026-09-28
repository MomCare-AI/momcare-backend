# AI Instruction Presets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single mutable `custom_instructions` text field at each AI Summary config
tier (platform, organization) with a history of named, activatable presets.

**Architecture:** One new model, `AIInstructionPreset` (`core/ai/models.py`), with a nullable
`organization` FK doubling as the tier marker (`NULL` = platform, set = that hospital). Two
service functions handle the "at most one active per scope" invariant transactionally. The prompt
builder reads instruction text through whichever preset is active per scope instead of the two
flat fields being removed. Two symmetric sets of four endpoints (list/create/activate/deactivate)
expose it per tier, reusing one shared serializer.

**Tech Stack:** Django 5, DRF, Postgres RLS (`SET LOCAL` + `FORCE ROW LEVEL SECURITY`), pytest-django.

**Spec:** `docs/design/2026-09-28-ai-instruction-presets-design.md`

## Global Constraints

- Presets are immutable after creation — no PATCH/edit endpoint, ever.
- Presets are never deleted — no DELETE endpoint, ever.
- `organization` on `AIInstructionPreset` is never accepted from a request body on either tier's
  create endpoint — the view sets it (`None` for platform, `request.user.organization` for org).
- At most one active preset per scope, enforced in the service layer inside one
  `transaction.atomic()` block, not a DB constraint.
- Activating an already-active preset, or deactivating an already-inactive one, is `400`.
- `AIProviderConfig.custom_instructions` and `Organization.ai_custom_instructions` are removed
  entirely once the data migration has copied any existing non-empty value into a first active
  preset — not kept alongside the new model.
- Cross-tenant and cross-tier lookups (an org's preset id against the platform endpoint, another
  hospital's preset id) are `404`, never `403`.
- `uv run pytest momcare_platform/core -q` must stay green after every task.

## Review Focus

- An empty-string `name` or `content` on create — the serializer must reject it (`blank=False` is
  the model/serializer default, but confirm a test actually exercises this, since every other
  field in this feature so far has been optional/blank-allowed).
- Deactivating the platform's only active preset, then generating a summary — `_build_prompt`
  must fall back to "no platform instructions" cleanly, not crash on a missing preset.
- Two presets active at once for the same scope must never exist even under a direct duplicate
  `activate` call in quick succession — Task 2's test proves the service function's transactional
  swap, not just "the happy path creates one row".
- A platform-tier preset (`organization=None`) must never appear in an organization's own list
  endpoint, and vice versa — Task 6/7's list tests both assert the other tier's presets are absent,
  not just that the caller's own presets are present.
- The data migration (Task 5) must handle an `Organization` with a **blank** `ai_custom_instructions`
  (the default) by creating no preset at all for that org — not an empty-content preset that would
  outrank "no org instructions" the moment anyone looked at the list.

---

### Task 1: `AIInstructionPreset` model

**Files:**
- Modify: `momcare_platform/core/ai/models.py`
- Create: `momcare_platform/core/ai/migrations/0003_aiinstructionpreset.py` (via `makemigrations`)
- Test: `momcare_platform/core/ai/tests/test_instruction_preset_model.py`

**Interfaces:**
- Produces: `AIInstructionPreset(organization: Organization | None, name: str, content: str, is_active: bool, activated_at: datetime | None, created_by: User | None, created_at, updated_at)` — consumed by Task 2 (service functions), Task 3 (`_build_prompt`), Task 5 (data migration), Task 6/7 (serializer + views).

- [ ] **Step 1: Write the failing test**

```python
"""AIInstructionPreset -- organization nullable doubles as the tier marker
(None = platform-wide, set = that hospital's own)."""

import pytest

from momcare_platform.core.ai.models import AIInstructionPreset

pytestmark = pytest.mark.django_db


def test_a_platform_tier_preset_has_no_organization(make_hospital):
    preset = AIInstructionPreset.objects.create(
        organization=None,
        name="Baseline",
        content="Always mention medication adherence.",
    )

    assert preset.organization is None
    assert preset.is_active is False
    assert preset.activated_at is None


def test_an_organization_tier_preset_carries_its_hospital(make_hospital):
    hospital = make_hospital("Preset Model Hospital")

    preset = AIInstructionPreset.objects.create(
        organization=hospital.org,
        name="Winter 2026",
        content="Emphasize hydration.",
    )

    assert preset.organization_id == hospital.org.id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest momcare_platform/core/ai/tests/test_instruction_preset_model.py -v`
Expected: FAIL with `ModuleNotFoundError` or `ImportError: cannot import name 'AIInstructionPreset'`.

- [ ] **Step 3: Add the model**

Append to `momcare_platform/core/ai/models.py`:

```python
class AIInstructionPreset(UUIDPrimaryKeyModel, TimeStampedModel):
    """A named, historical instruction text at one of two tiers --
    ``organization=None`` is platform-wide, a set ``organization`` is that
    hospital's own. Immutable once created and never deleted; "editing"
    means creating a new preset and activating it, "removing" means
    deactivating. See docs/design/2026-09-28-ai-instruction-presets-design.md.
    """

    organization = models.ForeignKey(
        "organization.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="ai_instruction_presets",
    )
    name = models.CharField(max_length=200)
    content = models.TextField()
    is_active = models.BooleanField(default=False)
    # Set every time this preset is activated; left untouched on
    # deactivation, so it always answers "when was this most recently made
    # active" even after it's no longer the active one.
    activated_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "AI Instruction Preset"
        verbose_name_plural = "AI Instruction Presets"

    def __str__(self) -> str:
        return self.name
```

- [ ] **Step 4: Generate and inspect the migration**

Run: `uv run python manage.py makemigrations ai`
Expected: `Migrations for 'ai': momcare_platform\core\ai\migrations\0003_aiinstructionpreset.py`

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest momcare_platform/core/ai/tests/test_instruction_preset_model.py -v`
Expected: `2 passed`

- [ ] **Step 6: Commit**

```bash
git add momcare_platform/core/ai/models.py momcare_platform/core/ai/migrations/0003_aiinstructionpreset.py momcare_platform/core/ai/tests/test_instruction_preset_model.py
git commit -m "feat(ai): add the AIInstructionPreset model"
```

---

### Task 2: Activate/deactivate service functions

**Files:**
- Modify: `momcare_platform/core/ai/services.py`
- Test: `momcare_platform/core/ai/tests/test_instruction_preset_activation.py`

**Interfaces:**
- Consumes: `AIInstructionPreset` (Task 1).
- Produces: `activate_instruction_preset(preset: AIInstructionPreset) -> None`,
  `deactivate_instruction_preset(preset: AIInstructionPreset) -> None` — both raise
  `InstructionPresetStateError` (new exception, same module) if the preset is already in the
  target state. Consumed by Task 6 (platform views) and Task 7 (organization views).

- [ ] **Step 1: Write the failing test**

```python
"""activate_instruction_preset()/deactivate_instruction_preset() -- the
transactional "at most one active per scope" swap."""

import pytest

from momcare_platform.core.ai.models import AIInstructionPreset
from momcare_platform.core.ai.services import (
    InstructionPresetStateError,
    activate_instruction_preset,
    deactivate_instruction_preset,
)

pytestmark = pytest.mark.django_db


def test_activating_a_preset_deactivates_the_previous_one_in_the_same_scope(make_hospital):
    hospital = make_hospital("Activation Swap Hospital")
    old = AIInstructionPreset.objects.create(
        organization=hospital.org, name="Old", content="Old text.", is_active=True,
    )
    new = AIInstructionPreset.objects.create(
        organization=hospital.org, name="New", content="New text.",
    )

    activate_instruction_preset(new)

    old.refresh_from_db()
    new.refresh_from_db()
    assert old.is_active is False
    assert new.is_active is True
    assert new.activated_at is not None


def test_activating_an_organization_preset_never_touches_the_platform_tier(make_hospital):
    hospital = make_hospital("Activation Isolation Hospital")
    platform_preset = AIInstructionPreset.objects.create(
        organization=None, name="Platform", content="Platform text.", is_active=True,
    )
    org_preset = AIInstructionPreset.objects.create(
        organization=hospital.org, name="Org", content="Org text.",
    )

    activate_instruction_preset(org_preset)

    platform_preset.refresh_from_db()
    assert platform_preset.is_active is True


def test_activating_an_organization_preset_never_touches_another_organizations(make_hospital):
    hospital_a = make_hospital("Activation Org A Hospital")
    hospital_b = make_hospital("Activation Org B Hospital")
    preset_a = AIInstructionPreset.objects.create(
        organization=hospital_a.org, name="A", content="A text.", is_active=True,
    )
    preset_b = AIInstructionPreset.objects.create(
        organization=hospital_b.org, name="B", content="B text.",
    )

    activate_instruction_preset(preset_b)

    preset_a.refresh_from_db()
    assert preset_a.is_active is True


def test_activating_an_already_active_preset_raises(make_hospital):
    hospital = make_hospital("Activation Already Active Hospital")
    preset = AIInstructionPreset.objects.create(
        organization=hospital.org, name="Active", content="Text.", is_active=True,
    )

    with pytest.raises(InstructionPresetStateError):
        activate_instruction_preset(preset)


def test_deactivating_clears_is_active_and_keeps_activated_at(make_hospital):
    from django.utils import timezone

    hospital = make_hospital("Deactivation Hospital")
    preset = AIInstructionPreset.objects.create(
        organization=hospital.org,
        name="Active",
        content="Text.",
        is_active=True,
        activated_at=timezone.now(),
    )

    deactivate_instruction_preset(preset)

    preset.refresh_from_db()
    assert preset.is_active is False
    assert preset.activated_at is not None


def test_deactivating_an_already_inactive_preset_raises(make_hospital):
    hospital = make_hospital("Deactivation Already Inactive Hospital")
    preset = AIInstructionPreset.objects.create(
        organization=hospital.org, name="Inactive", content="Text.",
    )

    with pytest.raises(InstructionPresetStateError):
        deactivate_instruction_preset(preset)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest momcare_platform/core/ai/tests/test_instruction_preset_activation.py -v`
Expected: FAIL with `ImportError: cannot import name 'activate_instruction_preset'`.

- [ ] **Step 3: Add the service functions**

Add to `momcare_platform/core/ai/services.py`, near the top (after the existing imports, before
`get_ai_config`):

```python
class InstructionPresetStateError(Exception):
    """Raised when an activate/deactivate call doesn't apply to the
    preset's current state -- e.g. activating one that's already active.
    A 400 at the view layer, never a silent no-op that could mask a
    caller bug like a double-click racing itself."""
```

Add near the bottom of the file (after `maybe_regenerate_for_risk_change`):

```python
def activate_instruction_preset(preset: AIInstructionPreset) -> None:
    """At most one active preset per scope. ``organization=preset.organization``
    scopes the "deactivate the others" step correctly for both tiers,
    including the platform tier (organization=None matches only other
    organization=None rows -- NULL never matches NULL in a WHERE clause via
    ``=``, but Django's ORM ``filter(organization=None)`` compiles to
    ``organization_id IS NULL``, not ``= NULL``, so this works)."""
    if preset.is_active:
        raise InstructionPresetStateError("This preset is already active.")
    with transaction.atomic():
        AIInstructionPreset.objects.filter(
            organization=preset.organization, is_active=True,
        ).exclude(pk=preset.pk).update(is_active=False)
        preset.is_active = True
        preset.activated_at = timezone.now()
        preset.save(update_fields=["is_active", "activated_at", "updated_at"])


def deactivate_instruction_preset(preset: AIInstructionPreset) -> None:
    if not preset.is_active:
        raise InstructionPresetStateError("This preset is not active.")
    preset.is_active = False
    preset.save(update_fields=["is_active", "updated_at"])
```

Add `AIInstructionPreset` to the existing model import line:

```python
from momcare_platform.core.ai.models import AIInstructionPreset, AIProviderConfig, AISummary
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest momcare_platform/core/ai/tests/test_instruction_preset_activation.py -v`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add momcare_platform/core/ai/services.py momcare_platform/core/ai/tests/test_instruction_preset_activation.py
git commit -m "feat(ai): add activate/deactivate for instruction presets"
```

---

### Task 3: `_build_prompt` reads through active presets

**Files:**
- Modify: `momcare_platform/core/ai/services.py`
- Modify: `momcare_platform/core/ai/tests/test_generate_patient_summary.py:63-72` (rewrite the one
  existing test that sets `ai_custom_instructions` directly)

**Interfaces:**
- Consumes: `AIInstructionPreset` (Task 1).
- Produces: `_build_prompt`'s instruction-reading behavior changes; `generate_patient_summary`'s
  own signature is unchanged. `Organization.ai_custom_instructions` and
  `AIProviderConfig.custom_instructions` are **not removed yet** in this task (Task 5 does that) —
  they simply stop being read, so nothing breaks mid-sequence if this task's commit is the last
  one to land for a while.

- [ ] **Step 1: Write the failing test**

Replace `momcare_platform/core/ai/tests/test_generate_patient_summary.py`'s existing
`test_the_word_cap_and_organization_instructions_reach_the_prompt` (lines 63-72) with:

```python
def test_the_word_cap_and_active_organization_preset_reach_the_prompt(patient):
    from momcare_platform.core.ai.models import AIInstructionPreset
    from momcare_platform.core.ai.services import activate_instruction_preset

    preset = AIInstructionPreset.objects.create(
        organization=patient.organization,
        name="Adherence",
        content="Always mention medication adherence.",
    )
    activate_instruction_preset(preset)

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        generate_patient_summary(patient)

    sent_prompt = mock_generate.call_args.args[0]
    assert "150" in sent_prompt
    assert "Always mention medication adherence." in sent_prompt


def test_an_inactive_organization_preset_never_reaches_the_prompt(patient):
    from momcare_platform.core.ai.models import AIInstructionPreset

    AIInstructionPreset.objects.create(
        organization=patient.organization,
        name="Never activated",
        content="Should never appear.",
    )

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        generate_patient_summary(patient)

    sent_prompt = mock_generate.call_args.args[0]
    assert "Should never appear." not in sent_prompt


def test_an_active_platform_preset_reaches_every_patients_prompt(patient):
    from momcare_platform.core.ai.models import AIInstructionPreset
    from momcare_platform.core.ai.services import activate_instruction_preset

    preset = AIInstructionPreset.objects.create(
        organization=None, name="Platform-wide", content="Always note the hospital's timezone.",
    )
    activate_instruction_preset(preset)

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        generate_patient_summary(patient)

    sent_prompt = mock_generate.call_args.args[0]
    assert "Always note the hospital's timezone." in sent_prompt


def test_deactivating_the_only_active_platform_preset_leaves_the_prompt_with_no_platform_section(patient):
    """Review Focus item: deactivating the platform's only active preset,
    then generating a summary, must fall back to "no platform instructions"
    cleanly -- not crash on a missing preset, and not keep sending stale
    content from the now-deactivated row."""
    from momcare_platform.core.ai.models import AIInstructionPreset
    from momcare_platform.core.ai.services import activate_instruction_preset, deactivate_instruction_preset

    preset = AIInstructionPreset.objects.create(
        organization=None, name="Platform-wide", content="Should disappear once deactivated.",
    )
    activate_instruction_preset(preset)
    deactivate_instruction_preset(preset)

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        generate_patient_summary(patient)

    sent_prompt = mock_generate.call_args.args[0]
    assert "Should disappear once deactivated." not in sent_prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest momcare_platform/core/ai/tests/test_generate_patient_summary.py -v`
Expected: FAIL on `test_the_word_cap_and_active_organization_preset_reach_the_prompt` and
`test_an_active_platform_preset_reaches_every_patients_prompt` — the preset's `content` never
reaches `sent_prompt` (still reading the old empty field). `test_an_inactive_organization_preset_never_reaches_the_prompt`
and `test_deactivating_the_only_active_platform_preset_leaves_the_prompt_with_no_platform_section`
both pass vacuously right now (which is why the other two matter — they're the ones proving the
wiring actually changed, not just that absence stays absent).

- [ ] **Step 3: Rewire `_build_prompt`'s two callers**

In `momcare_platform/core/ai/services.py`, replace the `generate_patient_summary` block that
builds `prompt`:

```python
        with read_scope():
            snapshot = _build_data_snapshot(patient)
            config = get_ai_config()
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
            prompt = _build_prompt(
                snapshot,
                config,
                platform_instructions,
                org_instructions,
                deactivated=deactivated,
            )
```

Update `_build_prompt`'s signature and body:

```python
def _build_prompt(
    snapshot: dict,
    config: AIProviderConfig,
    platform_instructions: str,
    org_instructions: str,
    *,
    deactivated: bool,
) -> str:
    data_lines = "\n".join(f"- {key}: {_format_snapshot_value(value)}" for key, value in snapshot.items())
    sections = [
        _BASE_PROMPT.format(
            max_words=config.max_words,
            data_lines=data_lines,
            closing_instruction=_DEACTIVATED_CLOSING if deactivated else _ACTIVE_CLOSING,
        ),
    ]
    if platform_instructions:
        sections.append(platform_instructions)
    if org_instructions:
        sections.append(org_instructions)
    return "\n\n".join(sections)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest momcare_platform/core/ai/tests/test_generate_patient_summary.py -v`
Expected: `10 passed` (the 6 pre-existing tests, minus the 1 replaced
(`test_the_word_cap_and_organization_instructions_reach_the_prompt`), plus the 5 new ones this
step added: 6 - 1 + 5 = 10 — recount against the file's actual test functions before treating a
different number as a failure).

- [ ] **Step 5: Run the whole core.ai suite to confirm nothing else broke**

Run: `uv run pytest momcare_platform/core/ai -q`
Expected: all passed, `0 failed`.

- [ ] **Step 6: Commit**

```bash
git add momcare_platform/core/ai/services.py momcare_platform/core/ai/tests/test_generate_patient_summary.py
git commit -m "feat(ai): read instruction text through active presets, not the flat fields"
```

---

### Task 4: Row-Level Security for `ai_aiinstructionpreset`

**Files:**
- Create: `momcare_platform/core/organization/migrations/0028_ai_instruction_preset_row_level_security.py`
- Test: `momcare_platform/core/ai/tests/test_instruction_preset_rls.py`

**Interfaces:**
- Consumes: `AIInstructionPreset` (Task 1), `bypass_rls`/RLS session-variable mechanics
  (`core/common/rls.py`, unchanged).

**This task's test follows `momcare_platform/core/ai/tests/test_rls.py` (AISummary's own RLS
test) exactly, not an invented pattern.** That file doesn't attempt to switch to a non-bypassing
role or exercise the policy behaviorally from pytest at all — CLAUDE.md's Tenancy section explains
why: `local.py`/`test.py` use one ordinary role with `BYPASSRLS`, so a pytest session can never
observe RLS actually blocking anything, no matter what role-switching is attempted inside a test.
Both of AISummary's tests just check Postgres's own catalog (`pg_class`/`pg_policy`) for "is RLS
enabled/forced" and "does the policy exist" — confirming the migration did what it claims, not
confirming enforcement. Real enforcement against a genuine non-bypassing role is `scripts/verify_rls.py`'s
job (Step 5 below), run directly, not through pytest.

- [ ] **Step 1: Write the failing test**

```python
"""RLS for ai_aiinstructionpreset -- same verification shape as
core/ai/tests/test_rls.py (AISummary's own): confirms the policy exists and
is enforced/forced at the Postgres catalog level. Behavioral enforcement
against a real non-bypassing role is scripts/verify_rls.py's job, not
pytest's -- see that file's own docstring and CLAUDE.md's Tenancy section
for why a pytest session (running as a BYPASSRLS role locally) can never
observe RLS actually blocking anything."""

import pytest
from django.db import connection

pytestmark = pytest.mark.django_db


def test_instruction_preset_table_has_rls_enabled_and_forced():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'ai_aiinstructionpreset'",
        )
        row_security, forced = cursor.fetchone()

    assert row_security is True
    assert forced is True


def test_instruction_preset_table_has_the_tenant_isolation_policy():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT polname FROM pg_policy WHERE polrelid = 'ai_aiinstructionpreset'::regclass",
        )
        policy_names = {row[0] for row in cursor.fetchall()}

    assert "tenant_isolation" in policy_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest momcare_platform/core/ai/tests/test_instruction_preset_rls.py -v`
Expected: FAIL — `pg_class`/`pg_policy` return no matching row yet, since `ai_aiinstructionpreset`
doesn't exist until Task 1's migration runs, and even once it does, no RLS policy has been created
on it yet at this point in the plan.

- [ ] **Step 3: Write the migration**

```python
"""Row-Level Security for AIInstructionPreset.

Same fail-closed design and bypass path as 0006/0020/0023/0024/0025/0027;
see 0006's docstring for why NULLIF / SET LOCAL / FORCE are each necessary.

organization is a direct column, same shape as 0006's monitoring_device
policy -- and since NULL never equals a real uuid via `=`, a platform-tier
row (organization_id IS NULL) is invisible to every hospital session without
any special-case clause: the USING condition simply never matches it.
"""

from django.db import migrations

_BYPASS = "current_setting('app.rls_bypass', true) = 'on'"

_POLICIES = [
    (
        "ai_aiinstructionpreset",
        "organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid",
    ),
]


def _enable_sql() -> str:
    statements = []
    for table, using in _POLICIES:
        condition = f"({using}) OR {_BYPASS}"
        statements.extend(
            [
                f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;",
                f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;",
                f"CREATE POLICY tenant_isolation ON {table} FOR ALL USING ({condition});",
            ],
        )
    return "\n".join(statements)


def _disable_sql() -> str:
    statements = []
    for table, _using in _POLICIES:
        statements.extend(
            [
                f"DROP POLICY IF EXISTS tenant_isolation ON {table};",
                f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;",
                f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;",
            ],
        )
    return "\n".join(statements)


class Migration(migrations.Migration):
    dependencies = [
        ("organization", "0027_ai_summary_row_level_security"),
        ("ai", "0003_aiinstructionpreset"),
    ]

    operations = [
        migrations.RunSQL(sql=_enable_sql(), reverse_sql=_disable_sql()),
    ]
```

- [ ] **Step 4: Apply and run test to verify it passes**

Run: `uv run python manage.py migrate && uv run pytest momcare_platform/core/ai/tests/test_instruction_preset_rls.py -v`
Expected: `2 passed`

- [ ] **Step 5: Run `scripts/verify_rls.py` to confirm the new policy holds against the real non-bypassing role**

Run: `uv run python scripts/verify_rls.py`
Expected: no failures reported for `ai_aiinstructionpreset` (read that script's own output format —
it's not a pytest test, see its docstring referenced in CLAUDE.md's Tenancy section).

- [ ] **Step 6: Commit**

```bash
git add momcare_platform/core/organization/migrations/0028_ai_instruction_preset_row_level_security.py momcare_platform/core/ai/tests/test_instruction_preset_rls.py
git commit -m "feat(ai): enable Row-Level Security on ai_aiinstructionpreset"
```

---

### Task 5: Backfill and remove the old fields

**Files:**
- Create: `momcare_platform/core/ai/migrations/0004_migrate_instructions_to_presets.py`
- Create: `momcare_platform/core/ai/migrations/0005_remove_aiproviderconfig_custom_instructions.py` (via `makemigrations`)
- Create: `momcare_platform/core/organization/migrations/0029_remove_organization_ai_custom_instructions.py` (via `makemigrations`)
- Modify: `momcare_platform/core/ai/models.py` (remove `AIProviderConfig.custom_instructions`)
- Modify: `momcare_platform/core/organization/models.py` (remove `Organization.ai_custom_instructions`)
- Modify: `momcare_platform/core/platform_admin/api/serializers.py` (drop `custom_instructions` from `AIProviderConfigSerializer.Meta.fields`)
- Modify: `momcare_platform/core/organization/api/serializers.py` (delete `OrganizationAIInstructionsSerializer`)
- Modify: `momcare_platform/core/organization/api/views.py` (delete `OrganizationAIInstructionsView`)
- Modify: `config/api_router.py` (remove the `organization-ai-instructions` route and its import)
- Delete: `momcare_platform/core/organization/tests/api/test_ai_instructions.py`
- Delete: `momcare_platform/core/organization/tests/test_ai_custom_instructions.py`

**Interfaces:**
- Consumes: `AIInstructionPreset` (Task 1).
- Produces: `AIProviderConfig.custom_instructions` and `Organization.ai_custom_instructions` no
  longer exist anywhere in the codebase after this task — Task 6/7 write only against
  `AIInstructionPreset`.

**No dedicated pytest test for the data migration itself** — this deliberately follows this
codebase's own existing convention: every prior `RunPython` data migration
(`core/locations/migrations/0004_backfill_main_branch.py`,
`core/organization/migrations/0010_migrate_files_to_database_storage.py`,
`core/patients/migrations/0007_patient_organization_and_cnic.py`, several more) writes its
forward/backward functions inline using `apps.get_model()` historical models and has no
corresponding test file anywhere in the tree — confirmed by checking each one's app's `tests/`
directory before writing this task. `pyproject.toml` already excludes `*/migrations/*` from
coverage and from strict mypy checking for the same reason. Correctness here is verified by
actually running the migration against representative data (Step 2 below) and by the full test
suite passing afterward with the old fields gone (Step 5) — not by a unit test importing
migration internals, which would itself be a pattern with no precedent in this codebase.

- [ ] **Step 1: Write the data migration**

Create `momcare_platform/core/ai/migrations/0004_migrate_instructions_to_presets.py`:

```python
"""Copy any existing non-empty AIProviderConfig.custom_instructions /
Organization.ai_custom_instructions into a first active AIInstructionPreset
per scope, before 0005/0029 drop those two fields for good. A blank value
(the default at both tiers) creates no preset -- an empty-content preset
would silently outrank "no instructions at all" the moment anyone looked at
the resulting list.

Reversal is deliberately a no-op: presets are never deleted anywhere else in
this feature, and by the time anyone reverses this migration a preset it
created may already have been deactivated or superseded by a newer one --
deleting it here would be a different kind of data loss than the one this
migration exists to prevent.
"""

from django.db import migrations
from django.utils import timezone


def _forwards(apps, schema_editor):
    AIProviderConfig = apps.get_model("ai", "AIProviderConfig")
    AIInstructionPreset = apps.get_model("ai", "AIInstructionPreset")
    Organization = apps.get_model("organization", "Organization")

    config = AIProviderConfig.objects.first()
    if config is not None and config.custom_instructions:
        AIInstructionPreset.objects.create(
            organization=None,
            name="Migrated instructions",
            content=config.custom_instructions,
            is_active=True,
            activated_at=timezone.now(),
        )

    for org in Organization.objects.exclude(ai_custom_instructions=""):
        AIInstructionPreset.objects.create(
            organization=org,
            name="Migrated instructions",
            content=org.ai_custom_instructions,
            is_active=True,
            activated_at=timezone.now(),
        )


def _backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0003_aiinstructionpreset"),
        ("organization", "0026_organization_ai_custom_instructions"),
    ]

    operations = [
        migrations.RunPython(_forwards, _backwards),
    ]
```

- [ ] **Step 2: Apply it against representative pre-existing data and verify by hand**

The database is already at migration state `ai 0003`/`organization 0028` at this point in the
plan (Task 4's last step applied through there), so this migration hasn't run yet. Run (creates
data the migration should pick up, applies 0004, then confirms the result):

```bash
uv run python manage.py shell -c "
from momcare_platform.core.ai.services import get_ai_config
from momcare_platform.core.organization.models import Organization
config = get_ai_config()
config.custom_instructions = 'Always mention medication adherence.'
config.save(update_fields=['custom_instructions', 'updated_at'])
org = Organization.objects.first()
if org:
    org.ai_custom_instructions = 'Emphasize hydration.'
    org.save(update_fields=['ai_custom_instructions', 'updated_at'])
"
uv run python manage.py migrate ai 0004
uv run python manage.py shell -c "
from momcare_platform.core.ai.models import AIInstructionPreset
for p in AIInstructionPreset.objects.all():
    print(p.organization_id, p.name, p.is_active, repr(p.content))
"
```

Expected: the second shell command prints one row with `organization_id=None` and
`content='Always mention medication adherence.'`, plus (if a test/dev database already had an
`Organization` row) a second row with that org's id and `content='Emphasize hydration.'` — both
`is_active=True`. Delete this manually-created data afterward with
`uv run python manage.py shell -c "from momcare_platform.core.ai.models import AIInstructionPreset; AIInstructionPreset.objects.all().delete()"`
if working against a persistent dev database rather than the disposable pytest test database —
this step is a hand-verification, not something the suite depends on existing afterward.

- [ ] **Step 3: Apply the data migration, then remove the old fields and everything that reads them**

Run: `uv run python manage.py migrate ai 0004`

In `momcare_platform/core/ai/models.py`, delete the `custom_instructions = models.TextField(blank=True)`
line from `AIProviderConfig`.

In `momcare_platform/core/organization/models.py`, delete the `ai_custom_instructions` field and
its preceding comment block (lines 135-140 as read at plan-writing time — confirm the exact lines
in the file before deleting, since earlier tasks in other work may have shifted them).

In `momcare_platform/core/platform_admin/api/serializers.py`, change
`AIProviderConfigSerializer.Meta.fields` to `["current_model", "max_words"]`.

Delete `OrganizationAIInstructionsSerializer` entirely from
`momcare_platform/core/organization/api/serializers.py`.

Delete `OrganizationAIInstructionsView` entirely from
`momcare_platform/core/organization/api/views.py`, and remove
`OrganizationAIInstructionsSerializer` from that file's import block.

In `config/api_router.py`: remove `OrganizationAIInstructionsView` from the import list at line 34,
and remove the `re_path(r"^organization/me/ai-instructions/?$", ...)` block (lines 190-194 as read
at plan-writing time — confirm before deleting).

Delete the two obsolete test files:

```bash
rm momcare_platform/core/organization/tests/api/test_ai_instructions.py
rm momcare_platform/core/organization/tests/test_ai_custom_instructions.py
```

- [ ] **Step 4: Generate the two schema-removal migrations**

Run: `uv run python manage.py makemigrations ai organization`
Expected: `Migrations for 'ai': ...0005_remove_aiproviderconfig_custom_instructions.py` and
`Migrations for 'organization': ...0029_remove_organization_ai_custom_instructions.py`.

Open the generated `organization` migration and confirm its `dependencies` include
`("ai", "0004_migrate_instructions_to_presets")` — if `makemigrations` didn't add that dependency
automatically (it has no reason to know about the cross-app data migration), add it by hand so the
backfill is guaranteed to run before this field is dropped:

```python
    dependencies = [
        ("organization", "0028_ai_instruction_preset_row_level_security"),
        ("ai", "0004_migrate_instructions_to_presets"),
    ]
```

- [ ] **Step 5: Apply migrations and run the full core.ai + core.organization + core.platform_admin suites**

Run: `uv run python manage.py migrate`
Run: `uv run pytest momcare_platform/core/ai momcare_platform/core/organization momcare_platform/core/platform_admin -q`
Expected: all passed, `0 failed` — no test anywhere still references
`ai_custom_instructions`/`custom_instructions` on either model, and none still hits
`/api/organization/me/ai-instructions/`.

- [ ] **Step 6: Commit**

```bash
git add momcare_platform/core/ai momcare_platform/core/organization momcare_platform/core/platform_admin config/api_router.py
git commit -m "feat(ai): backfill and remove the old single-instruction fields"
```

---

### Task 6: Platform-tier preset endpoints

**Files:**
- Create: `momcare_platform/core/ai/api/serializers.py:AIInstructionPresetSerializer` (add to
  existing file, shared by both tiers)
- Modify: `momcare_platform/core/platform_admin/api/views.py`
- Modify: `config/api_router.py`
- Test: `momcare_platform/core/platform_admin/tests/api/test_ai_instruction_presets.py`

**Interfaces:**
- Consumes: `AIInstructionPreset` (Task 1), `activate_instruction_preset`/
  `deactivate_instruction_preset`/`InstructionPresetStateError` (Task 2).
- Produces: `AIInstructionPresetSerializer` — consumed by Task 7 too (imported, not duplicated).

- [ ] **Step 1: Write the failing test**

```python
"""Platform-tier instruction presets -- list/create/activate/deactivate
under /api/platform-admin/ai-config/instruction-presets/. ROLE_PLATFORM_ADMIN
only, same gate as AIProviderConfigView."""

import json

import pytest
from django.conf import settings

from momcare_platform.core.ai.models import AIInstructionPreset
from momcare_platform.core.users.models import Role, User

pytestmark = pytest.mark.django_db

PRESETS_URL = "/api/platform-admin/ai-config/instruction-presets/"


@pytest.fixture
def platform_admin_auth(client):
    User.objects.create_user(
        email="presets-root@momcare.test",
        password="TestPass!2026",
        first_name="Root",
        last_name="Admin",
        role=Role.objects.get(code=settings.ROLE_PLATFORM_ADMIN),
        is_email_verified=True,
    )
    response = client.post(
        "/api/auth/login/",
        data={"email": "presets-root@momcare.test", "password": "TestPass!2026"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return {"HTTP_AUTHORIZATION": f"Bearer {response.json()['access']}"}


def test_platform_admin_can_create_a_preset(client, platform_admin_auth):
    response = client.post(
        PRESETS_URL,
        data=json.dumps({"name": "Baseline", "content": "Always mention medication adherence."}),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 201
    preset = AIInstructionPreset.objects.get()
    assert preset.organization is None
    assert preset.content == "Always mention medication adherence."
    assert preset.is_active is False


def test_organization_in_the_request_body_is_ignored(client, platform_admin_auth, make_hospital):
    hospital = make_hospital("Preset Spoof Hospital")

    response = client.post(
        PRESETS_URL,
        data=json.dumps({"name": "Sneaky", "content": "Text.", "organization": str(hospital.org.id)}),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 201
    assert AIInstructionPreset.objects.get().organization is None


def test_list_only_returns_platform_tier_presets(client, platform_admin_auth, make_hospital):
    hospital = make_hospital("Preset List Isolation Hospital")
    AIInstructionPreset.objects.create(organization=None, name="Platform", content="P.")
    AIInstructionPreset.objects.create(organization=hospital.org, name="Org", content="O.")

    response = client.get(PRESETS_URL, **platform_admin_auth)

    assert response.status_code == 200
    names = [row["name"] for row in response.json()["results"]]
    assert names == ["Platform"]


def test_activate_deactivates_the_previous_active_platform_preset(client, platform_admin_auth):
    old = AIInstructionPreset.objects.create(
        organization=None, name="Old", content="Old.", is_active=True,
    )
    new = AIInstructionPreset.objects.create(organization=None, name="New", content="New.")

    response = client.post(f"{PRESETS_URL}{new.id}/activate/", **platform_admin_auth)

    assert response.status_code == 200
    old.refresh_from_db()
    new.refresh_from_db()
    assert old.is_active is False
    assert new.is_active is True


def test_activating_an_already_active_preset_is_a_400(client, platform_admin_auth):
    preset = AIInstructionPreset.objects.create(
        organization=None, name="Active", content="Text.", is_active=True,
    )

    response = client.post(f"{PRESETS_URL}{preset.id}/activate/", **platform_admin_auth)

    assert response.status_code == 400


def test_deactivate_clears_is_active(client, platform_admin_auth):
    preset = AIInstructionPreset.objects.create(
        organization=None, name="Active", content="Text.", is_active=True,
    )

    response = client.post(f"{PRESETS_URL}{preset.id}/deactivate/", **platform_admin_auth)

    assert response.status_code == 200
    preset.refresh_from_db()
    assert preset.is_active is False


def test_activating_an_organizations_preset_via_the_platform_endpoint_is_404(client, platform_admin_auth, make_hospital):
    hospital = make_hospital("Preset Cross Tier Hospital")
    preset = AIInstructionPreset.objects.create(organization=hospital.org, name="Org", content="O.")

    response = client.post(f"{PRESETS_URL}{preset.id}/activate/", **platform_admin_auth)

    assert response.status_code == 404


def test_a_hospital_admin_is_refused(client, make_hospital, auth):
    hospital = make_hospital("Preset Platform Endpoint Refusal Hospital")

    response = client.get(PRESETS_URL, **auth(hospital.admin.email))

    assert response.status_code == 403


def test_a_blank_name_is_rejected(client, platform_admin_auth):
    """Review Focus item: name/content have no blank=True on the model, so
    DRF's ModelSerializer should already reject an empty one -- pinned here
    so a future change to the model's field options can't silently make a
    nameless preset possible."""
    response = client.post(
        PRESETS_URL,
        data=json.dumps({"name": "", "content": "Text."}),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 400
    assert not AIInstructionPreset.objects.exists()


def test_blank_content_is_rejected(client, platform_admin_auth):
    response = client.post(
        PRESETS_URL,
        data=json.dumps({"name": "Baseline", "content": ""}),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 400
    assert not AIInstructionPreset.objects.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest momcare_platform/core/platform_admin/tests/api/test_ai_instruction_presets.py -v`
Expected: FAIL with `404` on every request (URL doesn't exist yet) or `ImportError` if the test
file is collected before the URL — either way, none pass yet.

- [ ] **Step 3: Add the shared serializer**

Add to `momcare_platform/core/ai/api/serializers.py`:

```python
from momcare_platform.core.ai.models import AIInstructionPreset, AISummary


class AIInstructionPresetSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True, default="")

    class Meta:
        model = AIInstructionPreset
        fields = ["id", "name", "content", "is_active", "activated_at", "created_at", "created_by_name"]
        read_only_fields = ["is_active", "activated_at", "created_at", "created_by_name"]
```

(Adjust the existing `from momcare_platform.core.ai.models import AISummary` import line rather
than adding a second one.)

- [ ] **Step 4: Add the views**

Add to `momcare_platform/core/platform_admin/api/views.py`:

```python
from rest_framework.generics import get_object_or_404

from momcare_platform.core.ai.api.serializers import AIInstructionPresetSerializer
from momcare_platform.core.ai.models import AIInstructionPreset
from momcare_platform.core.ai.services import (
    InstructionPresetStateError,
    activate_instruction_preset,
    deactivate_instruction_preset,
)
from momcare_platform.core.common.pagination import DefaultPagination


class AIInstructionPresetListCreateView(APIView):
    """Platform-tier instruction preset history. List/create, never
    edit/delete -- see AIInstructionPreset's own docstring."""

    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def get(self, request):
        presets = AIInstructionPreset.objects.filter(organization__isnull=True)
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(presets, request, view=self)
        return Response(paginator.get_paginated_response(AIInstructionPresetSerializer(page, many=True).data).data)

    def post(self, request):
        serializer = AIInstructionPresetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(organization=None, created_by=request.user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class AIInstructionPresetActivateView(APIView):
    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def post(self, request, preset_id):
        preset = get_object_or_404(AIInstructionPreset, pk=preset_id, organization__isnull=True)
        try:
            activate_instruction_preset(preset)
        except InstructionPresetStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(AIInstructionPresetSerializer(preset).data)


class AIInstructionPresetDeactivateView(APIView):
    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def post(self, request, preset_id):
        preset = get_object_or_404(AIInstructionPreset, pk=preset_id, organization__isnull=True)
        try:
            deactivate_instruction_preset(preset)
        except InstructionPresetStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(AIInstructionPresetSerializer(preset).data)
```

Add `status` to the existing `from rest_framework import status` import if not already present in
that file (it currently is not — `AIProviderConfigView` doesn't use it — add the import line).

- [ ] **Step 5: Wire the URLs**

In `config/api_router.py`, add after the existing `platform-admin-ai-available-models` entry:

```python
    re_path(
        r"^platform-admin/ai-config/instruction-presets/?$",
        AIInstructionPresetListCreateView.as_view(),
        name="platform-admin-instruction-presets",
    ),
    re_path(
        r"^platform-admin/ai-config/instruction-presets/(?P<preset_id>[0-9a-f-]+)/activate/?$",
        AIInstructionPresetActivateView.as_view(),
        name="platform-admin-instruction-preset-activate",
    ),
    re_path(
        r"^platform-admin/ai-config/instruction-presets/(?P<preset_id>[0-9a-f-]+)/deactivate/?$",
        AIInstructionPresetDeactivateView.as_view(),
        name="platform-admin-instruction-preset-deactivate",
    ),
```

Add the three new view names to the existing
`from momcare_platform.core.platform_admin.api.views import AIAvailableModelsView, AIProviderConfigView`
import line.

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest momcare_platform/core/platform_admin/tests/api/test_ai_instruction_presets.py -v`
Expected: `10 passed`

- [ ] **Step 7: Commit**

```bash
git add momcare_platform/core/ai/api/serializers.py momcare_platform/core/platform_admin/api/views.py config/api_router.py momcare_platform/core/platform_admin/tests/api/test_ai_instruction_presets.py
git commit -m "feat(platform_admin): add platform-tier instruction preset endpoints"
```

---

### Task 7: Organization-tier preset endpoints

**Files:**
- Modify: `momcare_platform/core/organization/api/views.py`
- Modify: `config/api_router.py`
- Test: `momcare_platform/core/organization/tests/api/test_ai_instruction_presets.py`

**Interfaces:**
- Consumes: `AIInstructionPresetSerializer` (Task 6), `activate_instruction_preset`/
  `deactivate_instruction_preset`/`InstructionPresetStateError` (Task 2).

- [ ] **Step 1: Write the failing test**

```python
"""Organization-tier instruction presets -- list/create/activate/deactivate
under /api/organization/me/instruction-presets/. hospital_admin only, same
gate the old ai-instructions endpoint used."""

import json

import pytest
from django.conf import settings

from momcare_platform.core.ai.models import AIInstructionPreset

pytestmark = pytest.mark.django_db

PRESETS_URL = "/api/organization/me/instruction-presets/"


def test_hospital_admin_can_create_a_preset(client, make_hospital, auth):
    hospital = make_hospital("Org Preset Create Hospital")

    response = client.post(
        PRESETS_URL,
        data=json.dumps({"name": "Winter 2026", "content": "Emphasize hydration."}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    preset = AIInstructionPreset.objects.get()
    assert preset.organization_id == hospital.org.id


def test_organization_in_the_request_body_is_ignored(client, make_hospital, auth):
    hospital = make_hospital("Org Preset Spoof Hospital")
    other_hospital = make_hospital("Org Preset Spoof Target Hospital")

    response = client.post(
        PRESETS_URL,
        data=json.dumps({"name": "Sneaky", "content": "Text.", "organization": str(other_hospital.org.id)}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    assert AIInstructionPreset.objects.get().organization_id == hospital.org.id


def test_a_provider_is_refused(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Org Preset Provider Refusal Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, email="provider@orgpresets.test")

    response = client.post(
        PRESETS_URL,
        data=json.dumps({"name": "Should not work", "content": "Text."}),
        content_type="application/json",
        **auth(provider.email),
    )

    assert response.status_code == 403


def test_list_only_returns_this_hospitals_own_presets(client, make_hospital, auth):
    hospital = make_hospital("Org Preset List Isolation Hospital")
    other_hospital = make_hospital("Org Preset List Other Hospital")
    AIInstructionPreset.objects.create(organization=hospital.org, name="Mine", content="M.")
    AIInstructionPreset.objects.create(organization=other_hospital.org, name="Theirs", content="T.")
    AIInstructionPreset.objects.create(organization=None, name="Platform", content="P.")

    response = client.get(PRESETS_URL, **auth(hospital.admin.email))

    assert response.status_code == 200
    names = [row["name"] for row in response.json()["results"]]
    assert names == ["Mine"]


def test_activate_deactivates_the_previous_active_preset_for_this_hospital_only(client, make_hospital, auth):
    hospital = make_hospital("Org Preset Activate Hospital")
    old = AIInstructionPreset.objects.create(
        organization=hospital.org, name="Old", content="Old.", is_active=True,
    )
    new = AIInstructionPreset.objects.create(organization=hospital.org, name="New", content="New.")

    response = client.post(f"{PRESETS_URL}{new.id}/activate/", **auth(hospital.admin.email))

    assert response.status_code == 200
    old.refresh_from_db()
    new.refresh_from_db()
    assert old.is_active is False
    assert new.is_active is True


def test_another_hospitals_preset_id_is_404_not_403(client, make_hospital, auth):
    hospital = make_hospital("Org Preset Cross Tenant Hospital")
    other_hospital = make_hospital("Org Preset Cross Tenant Other Hospital")
    preset = AIInstructionPreset.objects.create(organization=other_hospital.org, name="Theirs", content="T.")

    response = client.post(f"{PRESETS_URL}{preset.id}/activate/", **auth(hospital.admin.email))

    assert response.status_code == 404


def test_a_platform_tier_preset_id_is_404_via_the_organization_endpoint(client, make_hospital, auth):
    hospital = make_hospital("Org Preset Cross Tier Hospital")
    preset = AIInstructionPreset.objects.create(organization=None, name="Platform", content="P.")

    response = client.post(f"{PRESETS_URL}{preset.id}/deactivate/", **auth(hospital.admin.email))

    assert response.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest momcare_platform/core/organization/tests/api/test_ai_instruction_presets.py -v`
Expected: FAIL — every request 404s (URL doesn't exist) or `ImportError` on collection.

- [ ] **Step 3: Add the views**

Add to `momcare_platform/core/organization/api/views.py`, replacing the space where
`OrganizationAIInstructionsView` used to be (removed in Task 5):

```python
from rest_framework.generics import get_object_or_404

from momcare_platform.core.ai.api.serializers import AIInstructionPresetSerializer
from momcare_platform.core.ai.models import AIInstructionPreset
from momcare_platform.core.ai.services import (
    InstructionPresetStateError,
    activate_instruction_preset,
    deactivate_instruction_preset,
)


class OrganizationAIInstructionPresetListCreateView(APIView):
    """This hospital's own instruction preset history. hospital_admin only,
    same restriction the old ai-instructions endpoint used -- a setting that
    shapes a clinical-facing output shouldn't be editable by every staff
    role."""

    permission_classes = [IsAuthenticated, IsHospitalAdmin]

    def get(self, request):
        org = request.user.organization
        if org is None:
            return Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)
        presets = AIInstructionPreset.objects.filter(organization=org)
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(presets, request, view=self)
        return paginator.get_paginated_response(AIInstructionPresetSerializer(page, many=True).data)

    def post(self, request):
        org = request.user.organization
        if org is None:
            return Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)
        serializer = AIInstructionPresetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(organization=org, created_by=request.user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class OrganizationAIInstructionPresetActivateView(APIView):
    permission_classes = [IsAuthenticated, IsHospitalAdmin]

    def post(self, request, preset_id):
        org = request.user.organization
        if org is None:
            return Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)
        preset = get_object_or_404(AIInstructionPreset, pk=preset_id, organization=org)
        try:
            activate_instruction_preset(preset)
        except InstructionPresetStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(AIInstructionPresetSerializer(preset).data)


class OrganizationAIInstructionPresetDeactivateView(APIView):
    permission_classes = [IsAuthenticated, IsHospitalAdmin]

    def post(self, request, preset_id):
        org = request.user.organization
        if org is None:
            return Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)
        preset = get_object_or_404(AIInstructionPreset, pk=preset_id, organization=org)
        try:
            deactivate_instruction_preset(preset)
        except InstructionPresetStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(AIInstructionPresetSerializer(preset).data)
```

`DefaultPagination` is already imported at the top of this file (used by
`OrganizationAuditLogView`) — confirm before adding a second import line.

- [ ] **Step 4: Wire the URLs**

In `config/api_router.py`, add in the same place the old `organization-ai-instructions` route was
removed from in Task 5:

```python
    re_path(
        r"^organization/me/instruction-presets/?$",
        OrganizationAIInstructionPresetListCreateView.as_view(),
        name="organization-instruction-presets",
    ),
    re_path(
        r"^organization/me/instruction-presets/(?P<preset_id>[0-9a-f-]+)/activate/?$",
        OrganizationAIInstructionPresetActivateView.as_view(),
        name="organization-instruction-preset-activate",
    ),
    re_path(
        r"^organization/me/instruction-presets/(?P<preset_id>[0-9a-f-]+)/deactivate/?$",
        OrganizationAIInstructionPresetDeactivateView.as_view(),
        name="organization-instruction-preset-deactivate",
    ),
```

Add the three new view names to `config/api_router.py`'s existing
`from momcare_platform.core.organization.api.views import (...)` import block.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest momcare_platform/core/organization/tests/api/test_ai_instruction_presets.py -v`
Expected: `7 passed`

- [ ] **Step 6: Commit**

```bash
git add momcare_platform/core/organization/api/views.py config/api_router.py momcare_platform/core/organization/tests/api/test_ai_instruction_presets.py
git commit -m "feat(organization): add organization-tier instruction preset endpoints"
```

---

### Task 8: Final verification

**Files:** none (verification only).

- [ ] **Step 1: Full backend suite**

Run: `uv run pytest momcare_platform/core -q`
Expected: all passed, `0 failed`.

- [ ] **Step 2: Lint, types, import boundaries**

Run: `uv run ruff check .`
Expected: `All checks passed!`

Run: `uv run ruff format --check momcare_platform config`
Expected: no reformat needed on any touched file (a `docs/` markdown file reformat suggestion, if
any, is pre-existing and not this plan's concern).

Run: `uv run mypy momcare_platform/core/ai momcare_platform/core/organization momcare_platform/core/platform_admin`
Expected: `Success: no issues found`.

Run: `uv run lint-imports`
Expected: `Contracts: 2 kept, 0 broken.`

- [ ] **Step 3: Migration state is clean**

Run: `uv run python manage.py makemigrations --check --dry-run`
Expected: `No changes detected` (exit code 0).

Run: `uv run python manage.py check`
Expected: `System check identified no issues (0 silenced).`

- [ ] **Step 4: Confirm no dangling references to the removed fields**

Run: `grep -rn "ai_custom_instructions\|\.custom_instructions" momcare_platform config docs/design/2026-09-28-ai-instruction-presets-design.md`
Expected: zero matches in `momcare_platform`/`config` (the design docs for 2026-09-27 are allowed
to still mention the old fields historically — they're not code).

- [ ] **Step 5: Commit any fixes found in Steps 1-4**

If any step above found something to fix, fix it and commit:

```bash
git add -A momcare_platform config
git commit -m "chore(ai): fix verification failures from the AI Instruction Presets feature"
```

If nothing needed fixing, skip this step — there is nothing to commit.
