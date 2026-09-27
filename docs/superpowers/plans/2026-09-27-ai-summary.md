# AI Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a cached, per-patient AI-generated narrative summary (vitals, risk, care team, notes) shown on the patient overview, powered by OpenRouter, with the model/word-limit/instructions switchable at runtime by the platform admin and organization admins.

**Architecture:** A new `core/ai` Django app owns an `AISummary` cache row per patient, an `AIProviderConfig` singleton (model choice + word cap + platform-wide instructions), and a thin `httpx`-based OpenRouter client. Generation is never triggered by a user action — three signals (patient creation, a risk-level change, patient deactivation) plus one periodic management command are the only paths that call it. `core/platform_admin` gets its first real endpoint to manage the config; `core/organization` gets one new field for per-hospital instructions.

**Tech Stack:** Django 5, DRF, `httpx` (new), Postgres RLS, pytest-django.

**Spec:** `docs/design/2026-09-27-ai-summary-design.md` — read it before starting; this plan implements it task-by-task and does not repeat its reasoning.

## Global Constraints

- Word cap is **150 words**, enforced by a soft prompt instruction and a `max_tokens` backstop — never one without the other.
- **No PHI stripping** — the patient's real name and all clinical data go into the prompt as-is. This was a deliberate decision; do not add stripping logic.
- **No manual "regenerate" endpoint** — only the four triggers below may cause a generation call. Do not add a user-facing POST action.
- `core/ai` is a **core** app. Anything in `modules.pregnancy.vitals` (app_label `"monitoring"`) or `modules.pregnancy.alerts` (app_label `"alerts"`) must be reached via `django.apps.apps.get_model(...)` (for a model class) or `importlib.import_module(...)` (for a function) — never a top-level `from momcare_platform.modules... import ...`. This is enforced by the `core must not import modules` import-linter contract; a violation fails `uv run lint-imports`.
- `AISummary.patient` is a `OneToOneField` — one cached row per patient, overwritten via `update_or_create`, never a growing history table.
- All new endpoints under `IsAuthenticated` plus the specific role check named in each task — never left at the DRF default alone.
- Every OpenRouter call goes through `core/ai/openrouter_client.py` and is **best-effort**: it must never raise into its caller, matching `core/common/mail.py`'s existing convention.
- Run `uv run pytest momcare_platform/core/ai -q` after every task in this app, and the full `uv run pytest momcare_platform/core -q` before the final commit of this plan.

## Review Focus

- A patient with no pregnancy yet (just onboarded, no clinical data at all) — `generate_patient_summary()` must not crash; it must produce a snapshot with mostly-empty fields and a prompt that tells the model to state that plainly.
- Calling `generate_patient_summary()` twice in a row for the same patient — must still leave exactly one `AISummary` row (the `OneToOneField` plus `update_or_create` should already guarantee this; a test proves it rather than assuming it).
- A deactivated patient must be **excluded** from `refresh_ai_summaries`'s queryset, and its existing `AISummary` row must be left completely untouched by the exclusion (not deleted, not blanked).
- `PATCH /api/platform-admin/ai-config/` submitting a `current_model` value **not** present in `openrouter_client.list_available_models()`'s current return must be rejected with a 400, never silently saved.
- Every new endpoint must 403 for a role that isn't the one it's gated to (`IsHospitalStaff` reader on the patient endpoint refused to a patient's own login; `IsPlatformAdmin` config endpoints refused to a `hospital_admin`; the organization-instructions endpoint refused to a `provider`/`nurse`/`care_manager`) — not just "happy path succeeds."

---

### Task 1: `httpx` dependency and AI settings

**Files:**
- Modify: `pyproject.toml` (dependencies list)
- Modify: `config/settings/base.py` (append a new `# AI SUMMARY` section, same style as the existing `# RISK MODEL` section)

**Interfaces:**
- Produces: `settings.OPENROUTER_API_KEY`, `settings.MOMCARE_AI_SUMMARY_DEFAULT_MODEL`, `settings.MOMCARE_AI_SUMMARY_DEFAULT_MAX_WORDS`, `settings.MOMCARE_AI_SUMMARY_REFRESH_HOURS` — every later task in this plan reads these by name.

- [ ] **Step 1: Add `httpx` to `pyproject.toml`**

In the `dependencies = [...]` list (currently ends with `"numpy>=2.5.2",`), add a new line, keeping alphabetical-ish grouping loose like the rest of the list:

```toml
  "httpx>=0.28.0",
```

- [ ] **Step 2: Add the settings block**

Append to the end of `config/settings/base.py`:

```python
# AI SUMMARY
# ------------------------------------------------------------------------------
# OpenRouter is a single OpenAI-compatible gateway to many underlying models --
# see docs/design/2026-09-27-ai-summary-design.md for the full reasoning. The
# API key is a deploy-time secret; the *model choice* and *instructions* are
# runtime-editable via AIProviderConfig (core.ai.models), not read from here
# again after the config row's first creation.
OPENROUTER_API_KEY = env("DJANGO_OPENROUTER_API_KEY", default="")
# Seed values only -- used the first time AIProviderConfig's row is created.
MOMCARE_AI_SUMMARY_DEFAULT_MODEL = env("DJANGO_MOMCARE_AI_SUMMARY_DEFAULT_MODEL", default="google/gemini-2.0-flash-001")
MOMCARE_AI_SUMMARY_DEFAULT_MAX_WORDS = env.int("DJANGO_MOMCARE_AI_SUMMARY_DEFAULT_MAX_WORDS", default=150)
# How stale an AISummary must be before the periodic refresh command touches
# it. Low-stakes, easy to tune once real cost data exists.
MOMCARE_AI_SUMMARY_REFRESH_HOURS = env.int("DJANGO_MOMCARE_AI_SUMMARY_REFRESH_HOURS", default=4)
```

- [ ] **Step 3: Verify settings load cleanly**

Run: `uv run python manage.py check`
Expected: `System check identified no issues (0 silenced).`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml config/settings/base.py uv.lock
git commit -m "feat(ai): add httpx dependency and AI Summary settings"
```

(If `uv.lock` didn't change because `uv sync` wasn't run, omit it from the add — but run `uv sync` first so the lock file and the new dependency are consistent.)

---

### Task 2: `AIProviderConfig` model, migration, and `get_ai_config()`

**Files:**
- Modify: `momcare_platform/core/ai/models.py`
- Create: `momcare_platform/core/ai/migrations/0001_initial.py` (via `manage.py makemigrations`)
- Create: `momcare_platform/core/ai/services.py`
- Test: `momcare_platform/core/ai/tests/__init__.py`, `momcare_platform/core/ai/tests/test_services.py`

**Interfaces:**
- Produces: `AIProviderConfig` (fields: `current_model: str`, `max_words: int`, `custom_instructions: str`), `get_ai_config() -> AIProviderConfig`.

- [ ] **Step 1: Create the tests package**

```bash
mkdir -p momcare_platform/core/ai/tests
touch momcare_platform/core/ai/tests/__init__.py
```

- [ ] **Step 2: Write the failing test**

`momcare_platform/core/ai/tests/test_services.py`:

```python
"""AIProviderConfig singleton -- get_ai_config() is the only read path."""

import pytest

from momcare_platform.core.ai.models import AIProviderConfig
from momcare_platform.core.ai.services import get_ai_config

pytestmark = pytest.mark.django_db


def test_get_ai_config_creates_the_row_from_settings_on_first_call(settings):
    settings.MOMCARE_AI_SUMMARY_DEFAULT_MODEL = "google/gemini-2.0-flash-001"
    settings.MOMCARE_AI_SUMMARY_DEFAULT_MAX_WORDS = 150
    assert AIProviderConfig.objects.count() == 0

    config = get_ai_config()

    assert config.current_model == "google/gemini-2.0-flash-001"
    assert config.max_words == 150
    assert AIProviderConfig.objects.count() == 1


def test_get_ai_config_returns_the_existing_row_on_later_calls():
    AIProviderConfig.objects.create(current_model="deepseek/deepseek-chat", max_words=120)

    config = get_ai_config()

    assert config.current_model == "deepseek/deepseek-chat"
    assert AIProviderConfig.objects.count() == 1
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest momcare_platform/core/ai/tests/test_services.py -v`
Expected: FAIL with `ModuleNotFoundError` or `ImportError` (neither `models.AIProviderConfig` nor `services.get_ai_config` exist yet).

- [ ] **Step 4: Write the model**

`momcare_platform/core/ai/models.py`:

```python
from django.db import models

from momcare_platform.core.common.models import TimeStampedModel, UUIDPrimaryKeyModel


class AIProviderConfig(UUIDPrimaryKeyModel, TimeStampedModel):
    """Platform-wide AI Summary configuration -- exactly one row, read via
    ``services.get_ai_config()`` and never any other way. Editable at runtime
    by the platform admin (see core.platform_admin's new endpoint) -- model
    choice and word cap are cost/infra levers kept platform-only;
    ``custom_instructions`` is free text appended to every generation's
    prompt. See docs/design/2026-09-27-ai-summary-design.md.
    """

    current_model = models.CharField(
        max_length=200,
        help_text="An OpenRouter model id, e.g. 'google/gemini-2.0-flash-001'.",
    )
    max_words = models.PositiveIntegerField(default=150)
    custom_instructions = models.TextField(blank=True)

    def __str__(self) -> str:
        return f"AI config ({self.current_model})"


class AISummary(UUIDPrimaryKeyModel, TimeStampedModel):
    """One cached AI-generated narrative per patient -- see Task 4."""

    patient = models.OneToOneField(
        "patients.Patient",
        on_delete=models.CASCADE,
        related_name="ai_summary",
    )
    content = models.TextField()
    generated_at = models.DateTimeField()
    model_used = models.CharField(max_length=200)
    # The pregnancy's final_risk_level at the moment this row was generated --
    # compared against the *current* risk level on every new RiskAssessment to
    # decide whether a regeneration is warranted. Blank when the patient had
    # no pregnancy/assessment yet at generation time.
    risk_level_at_generation = models.CharField(max_length=20, blank=True)

    def __str__(self) -> str:
        return f"AI summary for {self.patient_id}"
```

(`AISummary` is defined here now, ahead of Task 4, since both models belong in the same file and this avoids a second migration touching the same file moments later — Task 4 only adds its own migration and RLS policy, the model class itself is already in place.)

- [ ] **Step 5: Write `get_ai_config()`**

`momcare_platform/core/ai/services.py`:

```python
from django.conf import settings

from momcare_platform.core.ai.models import AIProviderConfig


def get_ai_config() -> AIProviderConfig:
    """The single read path for AIProviderConfig -- creates the row from
    settings on first call, since none exists yet in a fresh database."""
    config = AIProviderConfig.objects.first()
    if config is not None:
        return config
    return AIProviderConfig.objects.create(
        current_model=settings.MOMCARE_AI_SUMMARY_DEFAULT_MODEL,
        max_words=settings.MOMCARE_AI_SUMMARY_DEFAULT_MAX_WORDS,
    )
```

- [ ] **Step 6: Generate and apply the migration**

Run:
```bash
uv run python manage.py makemigrations ai
uv run python manage.py migrate ai
```
Expected: a new `momcare_platform/core/ai/migrations/0001_initial.py` creating both `AIProviderConfig` and `AISummary` tables.

- [ ] **Step 7: Run test to verify it passes**

Run: `uv run pytest momcare_platform/core/ai/tests/test_services.py -v`
Expected: PASS (2 passed)

- [ ] **Step 8: Commit**

```bash
git add momcare_platform/core/ai/
git commit -m "feat(ai): add AIProviderConfig and AISummary models"
```

---

### Task 3: `Organization.ai_custom_instructions`

**Files:**
- Modify: `momcare_platform/core/organization/models.py`
- Create: `momcare_platform/core/organization/migrations/0026_organization_ai_custom_instructions.py` (via `makemigrations`)
- Test: `momcare_platform/core/organization/tests/test_ai_custom_instructions.py` (new file)

**Interfaces:**
- Produces: `Organization.ai_custom_instructions: str`

- [ ] **Step 1: Write the failing test**

`momcare_platform/core/organization/tests/test_ai_custom_instructions.py`:

```python
"""Organization.ai_custom_instructions -- a plain, blank-by-default column,
same shape as confidence_threshold's per-hospital override."""

import pytest

from momcare_platform.core.organization.models import Organization

pytestmark = pytest.mark.django_db


def test_ai_custom_instructions_defaults_to_blank(make_hospital):
    hospital = make_hospital("Instructions Default Hospital")

    assert hospital.org.ai_custom_instructions == ""


def test_ai_custom_instructions_can_be_set(make_hospital):
    hospital = make_hospital("Instructions Set Hospital")
    hospital.org.ai_custom_instructions = "Always mention medication adherence if a note references it."
    hospital.org.save(update_fields=["ai_custom_instructions", "updated_at"])

    hospital.org.refresh_from_db()
    assert hospital.org.ai_custom_instructions == "Always mention medication adherence if a note references it."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest momcare_platform/core/organization/tests/test_ai_custom_instructions.py -v`
Expected: FAIL — `AttributeError` or a Django `FieldError` (the field doesn't exist yet).

- [ ] **Step 3: Add the field**

In `momcare_platform/core/organization/models.py`, on the `Organization` class, immediately after the `confidence_threshold` field (before `class Meta:`):

```python
    # Free-text steering appended to every AI Summary prompt for this
    # hospital's patients only, on top of the platform-wide instructions --
    # same "hospital customizes its own operational details" pattern as
    # ClinicalTag/StatusLabel/NoteTemplate. See
    # docs/design/2026-09-27-ai-summary-design.md.
    ai_custom_instructions = models.TextField(blank=True, default="")
```

- [ ] **Step 4: Generate and apply the migration**

Run:
```bash
uv run python manage.py makemigrations organization
uv run python manage.py migrate organization
```
Expected: a new `momcare_platform/core/organization/migrations/0026_organization_ai_custom_instructions.py`.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest momcare_platform/core/organization/tests/test_ai_custom_instructions.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add momcare_platform/core/organization/
git commit -m "feat(organization): add ai_custom_instructions field"
```

---

### Task 4: `AISummary` Row-Level Security

**Files:**
- Create: `momcare_platform/core/organization/migrations/0027_ai_summary_row_level_security.py`
- Test: `momcare_platform/core/ai/tests/test_rls.py`

**Interfaces:**
- Consumes: `AISummary` (Task 2), the existing `_BYPASS` / policy pattern from `core/organization/migrations/0025_analytics_row_level_security.py`.

- [ ] **Step 1: Write the migration**

`momcare_platform/core/organization/migrations/0027_ai_summary_row_level_security.py`:

```python
"""Row-Level Security for AISummary.

Same fail-closed design and bypass path as 0006/0020/0023/0024/0025; see
0006's docstring for why NULLIF / SET LOCAL / FORCE are each necessary.

AISummary is reached through patients_patient, which carries organization_id
directly (denormalized, same as Device) -- same shape as 0025's
PatientAnalytics policy.
"""

from django.db import migrations

_BYPASS = "current_setting('app.rls_bypass', true) = 'on'"

_POLICIES = [
    (
        "ai_aisummary",
        """EXISTS (
            SELECT 1 FROM patients_patient p
            WHERE p.id = ai_aisummary.patient_id
              AND p.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        )""",
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
        ("organization", "0026_organization_ai_custom_instructions"),
        ("ai", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=_enable_sql(), reverse_sql=_disable_sql()),
    ]
```

- [ ] **Step 2: Apply the migration**

Run: `uv run python manage.py migrate organization`
Expected: migration `0027_ai_summary_row_level_security` applies with no errors.

- [ ] **Step 3: Write the test proving it, by fault injection**

`momcare_platform/core/ai/tests/test_rls.py`:

```python
"""RLS on ai_aisummary -- proven by fault injection, same pattern as the
project's other RLS tests: verify the policy exists and blocks a
non-bypassing role, not just that the migration ran without error."""

import pytest
from django.db import connection

pytestmark = pytest.mark.django_db


def test_ai_summary_table_has_rls_enabled_and_forced():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'ai_aisummary'",
        )
        row_security, forced = cursor.fetchone()

    assert row_security is True
    assert forced is True


def test_ai_summary_table_has_the_tenant_isolation_policy():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT polname FROM pg_policy WHERE polrelid = 'ai_aisummary'::regclass",
        )
        policy_names = {row[0] for row in cursor.fetchall()}

    assert "tenant_isolation" in policy_names
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest momcare_platform/core/ai/tests/test_rls.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add momcare_platform/core/organization/migrations/0027_ai_summary_row_level_security.py momcare_platform/core/ai/tests/test_rls.py
git commit -m "feat(ai): enable Row-Level Security on ai_aisummary"
```

---

### Task 5: `openrouter_client.py`

**Files:**
- Create: `momcare_platform/core/ai/openrouter_client.py`
- Test: `momcare_platform/core/ai/tests/test_openrouter_client.py`

**Interfaces:**
- Produces: `generate(prompt: str, *, model: str, max_tokens: int) -> str | None`, `list_available_models() -> list[dict] | None`. Every later task that needs a real OpenRouter call imports these two names and nothing else from this module.

- [ ] **Step 1: Write the failing tests**

`momcare_platform/core/ai/tests/test_openrouter_client.py`:

```python
"""Best-effort OpenRouter client -- mocked throughout, never a real network
call in this suite, same posture as core.common.mail's own tests."""

from unittest.mock import patch

import httpx
import pytest

from momcare_platform.core.ai.openrouter_client import generate, list_available_models


def _mock_response(json_data, status_code=200):
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return httpx.Response(status_code, json=json_data, request=request)


def test_generate_returns_the_model_text_on_success():
    payload = {"choices": [{"message": {"content": "A short summary."}}]}
    with patch("httpx.post", return_value=_mock_response(payload)):
        result = generate("some prompt", model="google/gemini-2.0-flash-001", max_tokens=400)

    assert result == "A short summary."


def test_generate_returns_none_on_http_error_and_never_raises():
    with patch("httpx.post", side_effect=httpx.ConnectTimeout("timed out")):
        result = generate("some prompt", model="google/gemini-2.0-flash-001", max_tokens=400)

    assert result is None


def test_generate_returns_none_on_malformed_response():
    with patch("httpx.post", return_value=_mock_response({"unexpected": "shape"})):
        result = generate("some prompt", model="google/gemini-2.0-flash-001", max_tokens=400)

    assert result is None


def test_list_available_models_returns_ids_and_metadata():
    payload = {
        "data": [
            {"id": "google/gemini-2.0-flash-001", "pricing": {"prompt": "0.0001"}, "context_length": 128000},
            {"id": "deepseek/deepseek-chat", "pricing": {"prompt": "0.0002"}, "context_length": 64000},
        ],
    }
    with patch("httpx.get", return_value=_mock_response(payload)):
        models = list_available_models()

    assert models == [
        {"id": "google/gemini-2.0-flash-001", "pricing": {"prompt": "0.0001"}, "context_length": 128000},
        {"id": "deepseek/deepseek-chat", "pricing": {"prompt": "0.0002"}, "context_length": 64000},
    ]


def test_list_available_models_returns_none_on_failure():
    with patch("httpx.get", side_effect=httpx.ConnectTimeout("timed out")):
        result = list_available_models()

    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest momcare_platform/core/ai/tests/test_openrouter_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'momcare_platform.core.ai.openrouter_client'`

- [ ] **Step 3: Write the client**

`momcare_platform/core/ai/openrouter_client.py`:

```python
"""Thin OpenRouter wrapper -- the only place this project makes an outbound
HTTP call to a third-party AI provider.

Best-effort, matching core.common.mail's existing convention: every function
here logs and returns None on any failure rather than raising. A clinician
seeing yesterday's cached AISummary beats seeing an error page.
"""

from __future__ import annotations

import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://openrouter.ai/api/v1"


def generate(prompt: str, *, model: str, max_tokens: int) -> str | None:
    """One chat-completion call. `model` is always passed in explicitly by
    the caller (read from AIProviderConfig) -- this function has zero
    model-specific branching, which is what makes switching models a
    zero-logic-change operation."""
    try:
        response = httpx.post(
            f"{_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception:
        logger.exception("OpenRouter generate() call failed (model=%s)", model)
        return None


def list_available_models() -> list[dict] | None:
    """The live model catalog -- used to validate AIProviderConfig.current_model
    on write (see core.platform_admin's config endpoint) and to power the
    platform-admin picker UI. Never trust a cached/hardcoded model list here;
    the whole point is catching a model that stopped being served."""
    try:
        response = httpx.get(f"{_BASE_URL}/models", timeout=10.0)
        response.raise_for_status()
        return [
            {"id": m["id"], "pricing": m.get("pricing"), "context_length": m.get("context_length")}
            for m in response.json()["data"]
        ]
    except Exception:
        logger.exception("OpenRouter list_available_models() call failed")
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest momcare_platform/core/ai/tests/test_openrouter_client.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add momcare_platform/core/ai/openrouter_client.py momcare_platform/core/ai/tests/test_openrouter_client.py
git commit -m "feat(ai): add OpenRouter client (generate + list_available_models)"
```

---

### Task 6: Data snapshot builder

**Files:**
- Modify: `momcare_platform/core/ai/services.py`
- Test: `momcare_platform/core/ai/tests/test_data_snapshot.py`

**Interfaces:**
- Consumes: `Patient`/`Pregnancy` (`core.patients.models`), `MonitoringNote`/`PatientStatus` (`core.monitoring.models`), `PatientAnalytics` (`core.analytics.models`), `humanize_days_ago` (`core.common.formatting`), `format_duration` (`core.monitoring.services`), `RiskAssessment`/`VitalReading` (via `apps.get_model("monitoring", ...)`), `Alert` (via `apps.get_model("alerts", "Alert")`), `compute_vitals_summary` (via `importlib` on `modules.pregnancy.vitals.services`).
- Produces: `_build_data_snapshot(patient) -> dict` — later tasks (prompt builder, Task 7) read this dict's exact keys: `patient_name`, `gestational_age`, `current_risk_level`, `risk_this_month`, `latest_readings`, `thirty_day_average`, `provider_name`, `nurse_name`, `care_manager_name`, `recent_note`, `recent_note_author`, `last_monitoring_contact_display`, `last_reading_display`, `monitoring_time_display`, `active_statuses`, `pending_risk_count`, `has_open_alert`.

- [ ] **Step 1: Write the failing tests**

`momcare_platform/core/ai/tests/test_data_snapshot.py`:

```python
"""_build_data_snapshot() -- the structured data handed to the LLM. Covers
both a data-rich patient and a brand-new one with almost nothing on file
(the Review Focus item: must not crash, must produce mostly-empty fields)."""

from datetime import timedelta

import pytest
from django.utils import timezone

from momcare_platform.core.ai.services import _build_data_snapshot
from momcare_platform.core.monitoring.models import MonitoringNote
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db

HIGH_VITALS = {
    "systolic_bp": 185,
    "diastolic_bp": 125,
    "heart_rate": 130,
    "body_temp_f": 103.0,
    "hemoglobin": 6.0,
    "blood_glucose": 250,
    "stress_score": 9,
    "phys_activity_score": 1,
}


@pytest.fixture
def patient_with_data(make_hospital, make_staff):
    from django.conf import settings

    from momcare_platform.modules.pregnancy.vitals.models import VitalReading

    hospital = make_hospital("Snapshot Data Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, email="provider@snapshotdata.test")
    patient = onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
        pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=28)},
    )
    pregnancy = patient.current_pregnancy
    pregnancy.provider = provider.staff
    pregnancy.save(update_fields=["provider", "updated_at"])

    VitalReading.objects.create(
        pregnancy=pregnancy,
        recorded_at=timezone.now(),
        source=VitalReading.SOURCE_MANUAL,
        **HIGH_VITALS,
    )
    MonitoringNote.objects.create(
        patient=patient,
        pregnancy=pregnancy,
        note="Routine check-in, patient reports feeling well.",
        added_by=hospital.admin,
    )
    return patient


@pytest.fixture
def brand_new_patient(make_hospital):
    hospital = make_hospital("Snapshot Empty Hospital")
    return onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Zara", "last_name": "Khan"},
    )


def test_snapshot_includes_current_risk_level_and_recent_note(patient_with_data):
    snapshot = _build_data_snapshot(patient_with_data)

    assert snapshot["current_risk_level"] == "high"
    assert snapshot["recent_note"] == "Routine check-in, patient reports feeling well."
    assert snapshot["provider_name"]


def test_snapshot_on_brand_new_patient_does_not_crash_and_is_mostly_empty(brand_new_patient):
    snapshot = _build_data_snapshot(brand_new_patient)

    assert snapshot["current_risk_level"] is None
    assert snapshot["gestational_age"] is None
    assert snapshot["recent_note"] is None
    assert snapshot["provider_name"] is None
    assert snapshot["active_statuses"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest momcare_platform/core/ai/tests/test_data_snapshot.py -v`
Expected: FAIL — `ImportError` (`_build_data_snapshot` doesn't exist yet).

- [ ] **Step 3: Write the snapshot builder**

Append to `momcare_platform/core/ai/services.py`:

```python
import importlib

from django.apps import apps as django_apps
from django.utils import timezone

from momcare_platform.core.analytics.models import PatientAnalytics
from momcare_platform.core.common.formatting import humanize_days_ago
from momcare_platform.core.monitoring.services import format_duration


def _build_data_snapshot(patient) -> dict:
    """Everything the prompt builder (Task 7) needs, gathered once. No
    narrative judgement happens here -- that's the model's job (see the
    design doc's "structured data in, free-form prose out" section)."""
    RiskAssessment = django_apps.get_model("monitoring", "RiskAssessment")
    VitalReading = django_apps.get_model("monitoring", "VitalReading")
    Alert = django_apps.get_model("alerts", "Alert")
    vitals_services = importlib.import_module("momcare_platform.modules.pregnancy.vitals.services")

    pregnancy = patient.current_pregnancy

    snapshot: dict = {
        "patient_name": patient.full_name,
        "gestational_age": pregnancy.gestational_age_long_display if pregnancy else None,
        "current_risk_level": None,
        "risk_this_month": None,
        "latest_readings": {},
        "thirty_day_average": {},
        "provider_name": pregnancy.provider.name if pregnancy and pregnancy.provider else None,
        "nurse_name": pregnancy.nurse.name if pregnancy and pregnancy.nurse else None,
        "care_manager_name": pregnancy.care_manager.name if pregnancy and pregnancy.care_manager else None,
        "recent_note": None,
        "recent_note_author": None,
        "last_monitoring_contact_display": humanize_days_ago(
            patient.last_monitoring_contact_at,
            patient.location.timezone,
        ),
        "last_reading_display": humanize_days_ago(patient.last_reading_at, patient.location.timezone),
        "monitoring_time_display": None,
        "active_statuses": list(patient.statuses.order_by("-created_at").values_list("name", flat=True)[:5]),
        "pending_risk_count": 0,
        "has_open_alert": False,
    }

    if pregnancy is not None:
        latest_assessment = RiskAssessment.objects.filter(pregnancy=pregnancy).order_by("-assessed_at").first()
        if latest_assessment is not None:
            snapshot["current_risk_level"] = latest_assessment.final_risk_level

        vitals_summary = vitals_services.compute_vitals_summary(pregnancy)
        snapshot["thirty_day_average"] = vitals_summary["last_30_days_average"]
        snapshot["risk_this_month"] = vitals_summary["risk_this_month"]

        latest_reading = VitalReading.objects.filter(pregnancy=pregnancy).order_by("-recorded_at").first()
        if latest_reading is not None:
            snapshot["latest_readings"] = {
                "systolic_bp": latest_reading.systolic_bp,
                "diastolic_bp": latest_reading.diastolic_bp,
                "heart_rate": latest_reading.heart_rate,
                "body_temp_f": latest_reading.body_temp_f,
                "blood_glucose": latest_reading.blood_glucose,
                "hemoglobin": latest_reading.hemoglobin,
            }

        pending = RiskAssessment.objects.filter(pregnancy=pregnancy, review_status=RiskAssessment.REVIEW_PENDING)
        snapshot["pending_risk_count"] = sum(1 for assessment in pending if assessment.needs_attention)

        snapshot["has_open_alert"] = Alert.objects.filter(pregnancy=pregnancy, status=Alert.STATUS_OPEN).exists()

    note = patient.monitoring_notes.order_by("-recorded_at").first()
    if note is not None:
        snapshot["recent_note"] = note.note
        snapshot["recent_note_author"] = note.added_by.full_name if note.added_by else None

    month_start = timezone.now().date().replace(day=1)
    analytics_row = PatientAnalytics.objects.filter(patient=patient, period_month=month_start).first()
    if analytics_row is not None:
        snapshot["monitoring_time_display"] = format_duration(analytics_row.monitoring_seconds)

    return snapshot
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest momcare_platform/core/ai/tests/test_data_snapshot.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Run `lint-imports` to confirm the boundary contract holds**

Run: `uv run lint-imports`
Expected: no violation reported for `core.ai` (it never has a top-level `from momcare_platform.modules...` import — only `apps.get_model` / `importlib.import_module`, which the AST-based check cannot see, exactly like `core.patients`'s equivalent call sites).

- [ ] **Step 6: Commit**

```bash
git add momcare_platform/core/ai/services.py momcare_platform/core/ai/tests/test_data_snapshot.py
git commit -m "feat(ai): add the per-patient data snapshot builder"
```

---

### Task 7: Prompt builder and `generate_patient_summary()`

**Files:**
- Modify: `momcare_platform/core/ai/services.py`
- Test: `momcare_platform/core/ai/tests/test_generate_patient_summary.py`

**Interfaces:**
- Consumes: `_build_data_snapshot` (Task 6), `get_ai_config` (Task 2), `openrouter_client.generate` (Task 5), `AISummary` (Task 2), `Organization.ai_custom_instructions` (Task 3).
- Produces: `generate_patient_summary(patient, *, deactivated: bool = False) -> None` — the single entry point every trigger in Tasks 8-10 calls.

- [ ] **Step 1: Write the failing tests**

`momcare_platform/core/ai/tests/test_generate_patient_summary.py`:

```python
"""generate_patient_summary() -- the prompt assembly + client call + upsert,
end to end, with the OpenRouter client mocked."""

from unittest.mock import patch

import pytest
from django.utils import timezone

from momcare_platform.core.ai.models import AISummary
from momcare_platform.core.ai.services import generate_patient_summary
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db


@pytest.fixture
def patient(make_hospital):
    hospital = make_hospital("Generate Summary Hospital")
    return onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Sana", "last_name": "Malik"},
    )


def test_generate_patient_summary_creates_the_ai_summary_row(patient):
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value="Sana has no readings yet this month.",
    ) as mock_generate:
        generate_patient_summary(patient)

    summary = AISummary.objects.get(patient=patient)
    assert summary.content == "Sana has no readings yet this month."
    assert mock_generate.called


def test_generate_patient_summary_is_a_no_op_when_the_client_fails(patient):
    AISummary.objects.create(
        patient=patient,
        content="Yesterday's summary.",
        generated_at=timezone.now(),
        model_used="google/gemini-2.0-flash-001",
    )

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        generate_patient_summary(patient)

    summary = AISummary.objects.get(patient=patient)
    assert summary.content == "Yesterday's summary."


def test_calling_it_twice_leaves_exactly_one_row(patient):
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="First."):
        generate_patient_summary(patient)
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Second."):
        generate_patient_summary(patient)

    assert AISummary.objects.filter(patient=patient).count() == 1
    assert AISummary.objects.get(patient=patient).content == "Second."


def test_the_word_cap_and_organization_instructions_reach_the_prompt(patient):
    patient.organization.ai_custom_instructions = "Always mention medication adherence."
    patient.organization.save(update_fields=["ai_custom_instructions", "updated_at"])

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        generate_patient_summary(patient)

    sent_prompt = mock_generate.call_args.args[0]
    assert "150" in sent_prompt
    assert "Always mention medication adherence." in sent_prompt


def test_deactivation_flag_changes_the_closing_instruction(patient):
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="ok") as mock_generate:
        generate_patient_summary(patient, deactivated=True)

    sent_prompt = mock_generate.call_args.args[0]
    assert "deactivated" in sent_prompt.lower()
    assert "recommendation" not in sent_prompt.lower() or "instead of a forward-looking recommendation" in sent_prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest momcare_platform/core/ai/tests/test_generate_patient_summary.py -v`
Expected: FAIL — `ImportError` (`generate_patient_summary` doesn't exist yet).

- [ ] **Step 3: Write the prompt builder and `generate_patient_summary()`**

Append to `momcare_platform/core/ai/services.py`:

```python
from django.db import transaction

from momcare_platform.core.ai import openrouter_client
from momcare_platform.core.patients.models import Patient

_BASE_PROMPT = """You are writing a short clinical summary for a hospital staff member about one pregnant patient. Use only the data given below -- never invent a value, a name, or an event that is not present. If something is missing (no care team assigned, no readings this period, no recent note), state that plainly instead of omitting it. Write in plain prose, no bullet points, no markdown. Keep the entire summary to at most {max_words} words.

Patient data:
{data_lines}

{closing_instruction}"""

_ACTIVE_CLOSING = "Close with exactly one recommendation, grounded specifically in the data above."
_DEACTIVATED_CLOSING = (
    "This patient has just been deactivated. Close the summary by stating that clearly, "
    "instead of a forward-looking recommendation -- recommending future monitoring for "
    "someone no longer being monitored would not make sense."
)


def _build_prompt(snapshot: dict, config, org_instructions: str, *, deactivated: bool) -> str:
    data_lines = "\n".join(
        f"- {key}: {value}" for key, value in snapshot.items() if value not in (None, "", [], {})
    )
    sections = [
        _BASE_PROMPT.format(
            max_words=config.max_words,
            data_lines=data_lines,
            closing_instruction=_DEACTIVATED_CLOSING if deactivated else _ACTIVE_CLOSING,
        ),
    ]
    if config.custom_instructions:
        sections.append(config.custom_instructions)
    if org_instructions:
        sections.append(org_instructions)
    return "\n\n".join(sections)


def _max_tokens_for(max_words: int) -> int:
    """Generous backstop, not a length target -- roughly 2 tokens per word
    plus headroom, so it only catches a model that truly ignores the
    word-count instruction rather than shaping length on its own."""
    return max_words * 2 + 100


def generate_patient_summary(patient, *, deactivated: bool = False) -> None:
    """The single entry point every trigger (patient creation, a risk-level
    change, deactivation, the periodic refresh command) calls. Best-effort:
    on any client failure the existing cached AISummary row, if any, is left
    untouched -- see openrouter_client.generate()'s own docstring."""
    with transaction.atomic():
        # Locks this patient's row for the duration of the call, so a cron
        # sweep and a risk-level-change signal landing at the same moment
        # cannot both proceed and race each other into two OpenRouter calls.
        Patient.objects.select_for_update().get(pk=patient.pk)

        snapshot = _build_data_snapshot(patient)
        config = get_ai_config()
        prompt = _build_prompt(
            snapshot,
            config,
            patient.organization.ai_custom_instructions,
            deactivated=deactivated,
        )

        content = openrouter_client.generate(
            prompt,
            model=config.current_model,
            max_tokens=_max_tokens_for(config.max_words),
        )
        if content is None:
            return

        AISummary.objects.update_or_create(
            patient=patient,
            defaults={
                "content": content,
                "generated_at": timezone.now(),
                "model_used": config.current_model,
                "risk_level_at_generation": snapshot["current_risk_level"] or "",
            },
        )
```

Add `from momcare_platform.core.ai.models import AISummary` to the existing import block at the top of the file (it currently only imports `AIProviderConfig`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest momcare_platform/core/ai/tests/test_generate_patient_summary.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add momcare_platform/core/ai/services.py momcare_platform/core/ai/tests/test_generate_patient_summary.py
git commit -m "feat(ai): add the prompt builder and generate_patient_summary()"
```

---

### Task 8: Enrollment trigger and risk-level-change trigger

**Files:**
- Create: `momcare_platform/core/ai/signals.py`
- Modify: `momcare_platform/core/ai/apps.py`
- Modify: `momcare_platform/core/ai/services.py` (add `maybe_regenerate_for_risk_change`)
- Test: `momcare_platform/core/ai/tests/test_triggers.py`

**Interfaces:**
- Consumes: `generate_patient_summary` (Task 7), `Patient` (`core.patients.models`), `RiskAssessment` (via `apps.get_model`).
- Produces: `maybe_regenerate_for_risk_change(risk_assessment) -> None`, the two signal receivers wired at app-load time.

- [ ] **Step 1: Write the failing tests**

`momcare_platform/core/ai/tests/test_triggers.py`:

```python
"""The two signal-based triggers: patient creation and a risk-level change.
No manual regenerate endpoint exists -- these, plus the periodic command
(Task 9) and the deactivation call (Task 10), are the only paths in."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from momcare_platform.core.ai.models import AISummary
from momcare_platform.core.patients.services import onboard_patient
from momcare_platform.modules.pregnancy.vitals.models import RiskAssessment, VitalReading

pytestmark = pytest.mark.django_db

HIGH_VITALS = {
    "systolic_bp": 185,
    "diastolic_bp": 125,
    "heart_rate": 130,
    "body_temp_f": 103.0,
    "hemoglobin": 6.0,
    "blood_glucose": 250,
    "stress_score": 9,
    "phys_activity_score": 1,
}
LOW_VITALS = {
    "systolic_bp": 110,
    "diastolic_bp": 70,
    "heart_rate": 75,
    "body_temp_f": 98.6,
    "hemoglobin": 12.0,
    "blood_glucose": 90,
    "stress_score": 2,
    "phys_activity_score": 7,
}


def test_creating_a_patient_generates_the_first_summary(make_hospital):
    hospital = make_hospital("Enrollment Trigger Hospital")

    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value="Newly enrolled, no data yet.",
    ) as mock_generate:
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Hina", "last_name": "Yousaf"},
        )

    assert mock_generate.called
    assert AISummary.objects.get(patient=patient).content == "Newly enrolled, no data yet."


def test_a_risk_level_change_triggers_a_regeneration(make_hospital):
    hospital = make_hospital("Risk Change Trigger Hospital")
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Enrollment summary."):
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Rabia", "last_name": "Anwar"},
            pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=28)},
        )
    pregnancy = patient.current_pregnancy
    AISummary.objects.filter(patient=patient).update(risk_level_at_generation=RiskAssessment.LEVEL_LOW)

    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value="Risk just went High.",
    ) as mock_generate:
        VitalReading.objects.create(
            pregnancy=pregnancy,
            recorded_at=timezone.now(),
            source=VitalReading.SOURCE_MANUAL,
            **HIGH_VITALS,
        )

    assert mock_generate.called
    assert AISummary.objects.get(patient=patient).content == "Risk just went High."


def test_a_reading_that_does_not_change_the_risk_level_does_not_trigger_a_regeneration(make_hospital):
    hospital = make_hospital("Risk Unchanged Hospital")
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Enrollment summary."):
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Nida", "last_name": "Chaudhry"},
            pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=20)},
        )
    pregnancy = patient.current_pregnancy
    AISummary.objects.filter(patient=patient).update(risk_level_at_generation=RiskAssessment.LEVEL_LOW)

    with patch("momcare_platform.core.ai.openrouter_client.generate") as mock_generate:
        VitalReading.objects.create(
            pregnancy=pregnancy,
            recorded_at=timezone.now(),
            source=VitalReading.SOURCE_MANUAL,
            **LOW_VITALS,
        )

    assert not mock_generate.called
    assert AISummary.objects.get(patient=patient).content == "Enrollment summary."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest momcare_platform/core/ai/tests/test_triggers.py -v`
Expected: FAIL — the enrollment test fails because no signal exists yet (`mock_generate.called` is `False`); the risk-change tests fail the same way.

- [ ] **Step 3: Write `maybe_regenerate_for_risk_change`**

Append to `momcare_platform/core/ai/services.py`:

```python
def maybe_regenerate_for_risk_change(risk_assessment) -> None:
    """Connected to RiskAssessment's post_save in AiConfig.ready() (see
    apps.py) -- fires on every reading (reassess_risk() writes one row per
    reading, unconditionally), but only actually regenerates when the level
    genuinely moved since the cached summary was written. Most routine
    readings don't change the risk level, so this stays cheap."""
    patient = risk_assessment.pregnancy.patient
    try:
        stored_level = patient.ai_summary.risk_level_at_generation
    except AISummary.DoesNotExist:
        stored_level = None
    if stored_level == risk_assessment.final_risk_level:
        return
    generate_patient_summary(patient)
```

- [ ] **Step 4: Write the signal receivers**

`momcare_platform/core/ai/signals.py`:

```python
"""Two of the four ways an AI Summary generation can be triggered -- see
docs/design/2026-09-27-ai-summary-design.md's Triggers section. The other
two are core/ai/management/commands/refresh_ai_summaries.py (periodic) and a
direct call inside core.patients.services.deactivate_patient() (one-time,
core-to-core, no signal needed there).

There is deliberately no manual "regenerate" endpoint -- these triggers, plus
the periodic command, are the only paths that may call generate_patient_summary().
"""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from momcare_platform.core.patients.models import Patient

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Patient, dispatch_uid="ai_generate_summary_on_patient_creation")
def generate_summary_on_patient_creation(sender, instance, created, **kwargs):
    if not created:
        return
    from momcare_platform.core.ai.services import generate_patient_summary  # noqa: PLC0415

    generate_patient_summary(instance)


def on_risk_assessment_saved(sender, instance, created, **kwargs):
    """Connected manually in AiConfig.ready(), not via @receiver -- its
    `sender` (RiskAssessment) lives in modules.pregnancy.vitals, which core
    must never import statically. See apps.py for the apps.get_model()
    resolution this depends on."""
    from momcare_platform.core.ai.services import maybe_regenerate_for_risk_change  # noqa: PLC0415

    maybe_regenerate_for_risk_change(instance)
```

- [ ] **Step 5: Wire the risk-assessment signal in `apps.py`**

Replace the contents of `momcare_platform/core/ai/apps.py`:

```python
from django.apps import AppConfig


class AiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "momcare_platform.core.ai"
    label = "ai"

    def ready(self):
        from django.apps import apps as django_apps  # noqa: PLC0415
        from django.db.models.signals import post_save  # noqa: PLC0415

        from momcare_platform.core.ai import signals  # noqa: PLC0415

        # RiskAssessment lives in modules.pregnancy.vitals, which core must
        # never import statically -- resolved via apps.get_model() instead,
        # the same escape hatch core.patients already uses for this exact
        # cross-boundary case (see CLAUDE.md's "core must not import
        # modules" section). Safe to call here: Django populates every app's
        # models before calling any app's ready().
        RiskAssessment = django_apps.get_model("monitoring", "RiskAssessment")
        post_save.connect(
            signals.on_risk_assessment_saved,
            sender=RiskAssessment,
            dispatch_uid="ai_risk_level_change_trigger",
        )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest momcare_platform/core/ai/tests/test_triggers.py -v`
Expected: PASS (3 passed)

- [ ] **Step 7: Run `lint-imports` again**

Run: `uv run lint-imports`
Expected: no violation — `signals.py` and `apps.py` only reach `modules.pregnancy.vitals` via `apps.get_model`.

- [ ] **Step 8: Commit**

```bash
git add momcare_platform/core/ai/signals.py momcare_platform/core/ai/apps.py momcare_platform/core/ai/services.py momcare_platform/core/ai/tests/test_triggers.py
git commit -m "feat(ai): add enrollment and risk-level-change triggers"
```

---

### Task 9: Periodic refresh command

**Files:**
- Create: `momcare_platform/core/ai/management/__init__.py`
- Create: `momcare_platform/core/ai/management/commands/__init__.py`
- Create: `momcare_platform/core/ai/management/commands/refresh_ai_summaries.py`
- Test: `momcare_platform/core/ai/tests/test_refresh_command.py`

**Interfaces:**
- Consumes: `generate_patient_summary` (Task 7), `AISummary` (Task 2), `Patient` (`core.patients.models`), `settings.MOMCARE_AI_SUMMARY_REFRESH_HOURS`.

- [ ] **Step 1: Create the management command package**

```bash
mkdir -p momcare_platform/core/ai/management/commands
touch momcare_platform/core/ai/management/__init__.py
touch momcare_platform/core/ai/management/commands/__init__.py
```

- [ ] **Step 2: Write the failing tests**

`momcare_platform/core/ai/tests/test_refresh_command.py`:

```python
"""refresh_ai_summaries -- the periodic safety net. Excludes deactivated
patients (Review Focus item); refreshes missing-or-stale summaries only."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone

from momcare_platform.core.ai.models import AISummary
from momcare_platform.core.patients.services import deactivate_patient, onboard_patient

pytestmark = pytest.mark.django_db


def test_refreshes_a_patient_with_no_summary_yet(make_hospital, settings):
    settings.MOMCARE_AI_SUMMARY_REFRESH_HOURS = 4
    hospital = make_hospital("Refresh Missing Hospital")
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Mahnoor", "last_name": "Ali"},
        )
    assert not AISummary.objects.filter(patient=patient).exists()

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Refreshed.") as mock_generate:
        call_command("refresh_ai_summaries")

    assert mock_generate.called
    assert AISummary.objects.get(patient=patient).content == "Refreshed."


def test_does_not_touch_a_summary_that_is_still_fresh(make_hospital, settings):
    settings.MOMCARE_AI_SUMMARY_REFRESH_HOURS = 4
    hospital = make_hospital("Refresh Fresh Hospital")
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Fresh content."):
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Iqra", "last_name": "Saeed"},
        )

    with patch("momcare_platform.core.ai.openrouter_client.generate") as mock_generate:
        call_command("refresh_ai_summaries")

    assert not mock_generate.called
    assert AISummary.objects.get(patient=patient).content == "Fresh content."


def test_skips_deactivated_patients(make_hospital, settings):
    settings.MOMCARE_AI_SUMMARY_REFRESH_HOURS = 4
    hospital = make_hospital("Refresh Deactivated Hospital")
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Original."):
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Warda", "last_name": "Iqbal"},
        )
    AISummary.objects.filter(patient=patient).update(
        generated_at=timezone.now() - timedelta(hours=10),
    )
    deactivate_patient(patient, by=hospital.admin)

    with patch("momcare_platform.core.ai.openrouter_client.generate") as mock_generate:
        call_command("refresh_ai_summaries")

    assert not mock_generate.called
    assert AISummary.objects.get(patient=patient).content == "Original."
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest momcare_platform/core/ai/tests/test_refresh_command.py -v`
Expected: FAIL — `django.core.management.base.CommandError` (unknown command `refresh_ai_summaries`).

- [ ] **Step 4: Write the command**

`momcare_platform/core/ai/management/commands/refresh_ai_summaries.py`:

```python
"""The periodic safety net for AI Summaries -- catches everything the
enrollment and risk-level-change triggers don't (new notes, care-team
changes, monitoring activity), on a shorter cadence than "once a day".

Run it on a schedule, same operational pattern as escalate_alerts:

    # Linux / macOS - crontab, every 4 hours
    0 */4 * * * cd /srv/momcare && uv run python manage.py refresh_ai_summaries

    # Windows - Task Scheduler, repeat every 4 hours
    schtasks /create /tn MomCareRefreshAISummaries /sc hourly /mo 4 ^
      /tr "cmd /c cd /d D:\\path\\to\\backend && uv run python manage.py refresh_ai_summaries"

Safe to run as often as you like: a summary generated moments ago is simply
skipped until it goes stale again.
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from momcare_platform.core.ai.services import generate_patient_summary
from momcare_platform.core.common.rls import bypass_rls
from momcare_platform.core.patients.models import Patient


class Command(BaseCommand):
    help = "Refresh any active patient's AI Summary that is missing or older than the configured staleness window."

    def handle(self, *args, **options):
        cutoff = timezone.now() - timezone.timedelta(hours=settings.MOMCARE_AI_SUMMARY_REFRESH_HOURS)

        # This command sweeps every hospital's active patients in one pass,
        # by design -- same sanctioned bypass escalate_alerts already uses.
        with bypass_rls():
            patients = Patient.objects.filter(is_active=True).filter(
                Q(ai_summary__isnull=True) | Q(ai_summary__generated_at__lt=cutoff),
            )
            refreshed = 0
            for patient in patients:
                generate_patient_summary(patient)
                refreshed += 1

        if refreshed:
            self.stdout.write(self.style.SUCCESS(f"Refreshed {refreshed} AI summary(ies)."))
        else:
            self.stdout.write("No AI summary was due for a refresh.")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest momcare_platform/core/ai/tests/test_refresh_command.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Commit**

```bash
git add momcare_platform/core/ai/management/ momcare_platform/core/ai/tests/test_refresh_command.py
git commit -m "feat(ai): add the refresh_ai_summaries periodic command"
```

---

### Task 10: Deactivation trigger

**Files:**
- Modify: `momcare_platform/core/patients/services.py` (`deactivate_patient`)
- Test: `momcare_platform/core/ai/tests/test_deactivation_trigger.py`

**Interfaces:**
- Consumes: `generate_patient_summary(patient, deactivated=True)` (Task 7).

- [ ] **Step 1: Write the failing test**

`momcare_platform/core/ai/tests/test_deactivation_trigger.py`:

```python
"""Deactivating a patient writes one final AI Summary noting the transition,
then nothing else touches it while she stays inactive (see Task 9's own
skip-deactivated-patients test for that second half)."""

from unittest.mock import patch

import pytest

from momcare_platform.core.ai.models import AISummary
from momcare_platform.core.patients.services import deactivate_patient, onboard_patient

pytestmark = pytest.mark.django_db


def test_deactivating_a_patient_generates_a_final_summary(make_hospital):
    hospital = make_hospital("Deactivation Trigger Hospital")
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Enrollment summary."):
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Farah", "last_name": "Nawaz"},
        )

    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value="Active until today. Now deactivated.",
    ) as mock_generate:
        deactivate_patient(patient, by=hospital.admin)

    sent_prompt = mock_generate.call_args.args[0]
    assert "deactivated" in sent_prompt.lower()
    assert AISummary.objects.get(patient=patient).content == "Active until today. Now deactivated."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest momcare_platform/core/ai/tests/test_deactivation_trigger.py -v`
Expected: FAIL — `mock_generate` is never called (`call_args` is `None`), so the assertion raises `AttributeError`.

- [ ] **Step 3: Add the call inside `deactivate_patient()`**

In `momcare_platform/core/patients/services.py`, modify `deactivate_patient()`:

```python
def deactivate_patient(patient: Patient, *, by=None, reason: str = "") -> Patient:
    """Deactivate a patient — never delete. Clinical records survive."""
    patient.deactivate(by=by, reason=reason)
    # core.ai is another core app -- no import-linter concern reaching it
    # directly, unlike modules.pregnancy.vitals elsewhere in this file.
    # Local import purely to avoid a hard top-level circular dependency
    # between core.patients and core.ai (core.ai's own services module
    # imports Patient from here for its select_for_update() lock).
    from momcare_platform.core.ai.services import generate_patient_summary  # noqa: PLC0415

    generate_patient_summary(patient, deactivated=True)
    return patient
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest momcare_platform/core/ai/tests/test_deactivation_trigger.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Run the full existing patients test suite to confirm no regression**

Run: `uv run pytest momcare_platform/core/patients -q`
Expected: all existing tests still pass — `deactivate_patient()`'s return value and side effects on `Patient` itself are unchanged; this only adds a call after them.

- [ ] **Step 6: Commit**

```bash
git add momcare_platform/core/patients/services.py momcare_platform/core/ai/tests/test_deactivation_trigger.py
git commit -m "feat(ai): trigger a final AI summary on patient deactivation"
```

---

### Task 11: Patient-facing `GET /api/patients/{id}/ai-summary/`

**Files:**
- Create: `momcare_platform/core/ai/api/__init__.py`
- Create: `momcare_platform/core/ai/api/serializers.py`
- Create: `momcare_platform/core/ai/api/views.py`
- Modify: `config/api_router.py`
- Test: `momcare_platform/core/ai/tests/api/__init__.py`, `momcare_platform/core/ai/tests/api/test_patient_ai_summary.py`

**Interfaces:**
- Consumes: `AISummary` (Task 2), `IsHospitalStaff` (`core.common.permissions`), `LocationScopedQuerysetMixin`/`OrganizationScopedQuerysetMixin` (`core.common.scoping`).
- Produces: URL name `patient-ai-summary`.

- [ ] **Step 1: Create the api package**

```bash
mkdir -p momcare_platform/core/ai/api momcare_platform/core/ai/tests/api
touch momcare_platform/core/ai/api/__init__.py
touch momcare_platform/core/ai/tests/api/__init__.py
```

- [ ] **Step 2: Write the failing tests**

`momcare_platform/core/ai/tests/api/test_patient_ai_summary.py`:

```python
"""GET /api/patients/{id}/ai-summary/ -- read-only, IsHospitalStaff-gated,
same tier as reading the rest of a patient's detail."""

from unittest.mock import patch

import pytest
from django.conf import settings

from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db


def summary_url(patient_id):
    return f"/api/patients/{patient_id}/ai-summary/"


@pytest.fixture
def patient(make_hospital):
    hospital = make_hospital("AI Summary Endpoint Hospital")
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Enrollment summary."):
        return onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Bushra", "last_name": "Tariq"},
        )


def test_hospital_staff_can_read_the_cached_summary(client, patient, auth):
    response = client.get(summary_url(patient.id), **auth(patient.organization.owner.email))

    assert response.status_code == 200
    assert response.json()["content"] == "Enrollment summary."


def test_returns_404_before_any_summary_exists(client, make_hospital, auth):
    hospital = make_hospital("AI Summary Missing Hospital")
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        patient_no_summary = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Amna", "last_name": "Riaz"},
        )

    response = client.get(summary_url(patient_no_summary.id), **auth(hospital.admin.email))

    assert response.status_code == 404


def test_a_patient_role_login_is_refused(client, patient, auth):
    from momcare_platform.core.users.models import Role, User

    User.objects.create_user(
        email="selfservice@aisummaryendpoint.test",
        password="TestPass!2026",
        first_name="Self",
        last_name="Service",
        role=Role.objects.get(code=settings.ROLE_PATIENT),
    )

    response = client.get(summary_url(patient.id), **auth("selfservice@aisummaryendpoint.test"))

    assert response.status_code == 403


def test_another_hospitals_admin_gets_404_not_403(client, make_hospital, patient, auth):
    other_hospital = make_hospital("AI Summary Other Hospital")

    response = client.get(summary_url(patient.id), **auth(other_hospital.admin.email))

    assert response.status_code == 404
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest momcare_platform/core/ai/tests/api/test_patient_ai_summary.py -v`
Expected: FAIL — 404s for every URL (nothing mounted yet).

- [ ] **Step 4: Write the serializer**

`momcare_platform/core/ai/api/serializers.py`:

```python
from rest_framework import serializers

from momcare_platform.core.ai.models import AISummary


class AISummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = AISummary
        fields = ["content", "generated_at", "model_used"]
```

- [ ] **Step 5: Write the view**

`momcare_platform/core/ai/api/views.py`:

```python
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.ai.api.serializers import AISummarySerializer
from momcare_platform.core.ai.models import AISummary
from momcare_platform.core.common.permissions import IsHospitalStaff
from momcare_platform.core.common.scoping import LocationScopedQuerysetMixin
from momcare_platform.core.patients.models import Patient


class PatientAISummaryView(LocationScopedQuerysetMixin, APIView):
    """Read the cached AI Summary for one patient. Never generates one on
    the fly -- see the design doc's Triggers section for the only paths
    that ever call generate_patient_summary()."""

    permission_classes = [IsAuthenticated, IsHospitalStaff]
    organization_lookup = "organization"
    location_lookup = "location"

    def get(self, request, patient_id):
        patients = self.scope_to_locations(Patient.objects.all())
        patient = get_object_or_404(patients, pk=patient_id)
        summary = AISummary.objects.filter(patient=patient).first()
        if summary is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(AISummarySerializer(summary).data)
```

- [ ] **Step 6: Mount the URL**

In `config/api_router.py`, add the import (in the existing `from momcare_platform.core.patients.api.views import (...)` block's neighborhood, as its own import line):

```python
from momcare_platform.core.ai.api.views import PatientAISummaryView
```

Add to `core_urlpatterns`, immediately after the `patient-detail` entry:

```python
    re_path(
        r"^patients/(?P<patient_id>[0-9a-f-]{36})/ai-summary/?$",
        PatientAISummaryView.as_view(),
        name="patient-ai-summary",
    ),
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest momcare_platform/core/ai/tests/api/test_patient_ai_summary.py -v`
Expected: PASS (5 passed)

- [ ] **Step 8: Commit**

```bash
git add momcare_platform/core/ai/api/ momcare_platform/core/ai/tests/api/ config/api_router.py
git commit -m "feat(ai): add GET /api/patients/{id}/ai-summary/"
```

---

### Task 12: Platform-admin config endpoints (first real `platform_admin` feature)

**Files:**
- Modify: `momcare_platform/core/platform_admin/api/serializers.py`
- Modify: `momcare_platform/core/platform_admin/api/views.py`
- Modify: `config/api_router.py`
- Create: `momcare_platform/core/platform_admin/tests/api/test_ai_config.py`

**Interfaces:**
- Consumes: `AIProviderConfig`/`get_ai_config` (Task 2), `openrouter_client.list_available_models` (Task 5), `IsPlatformAdmin` (`core.common.permissions`).
- Produces: URL names `platform-admin-ai-config`, `platform-admin-ai-available-models`.

- [ ] **Step 1: Write the failing tests**

`momcare_platform/core/platform_admin/tests/api/test_ai_config.py`:

```python
"""GET/PATCH /api/platform-admin/ai-config/ -- the first real capability
core.platform_admin has ever had. IsPlatformAdmin-only."""

import json
from unittest.mock import patch

import pytest
from django.conf import settings

from momcare_platform.core.ai.models import AIProviderConfig
from momcare_platform.core.users.models import Role, User

pytestmark = pytest.mark.django_db

CONFIG_URL = "/api/platform-admin/ai-config/"
MODELS_URL = "/api/platform-admin/ai-config/available-models/"

_CATALOG = [
    {"id": "google/gemini-2.0-flash-001", "pricing": {"prompt": "0.0001"}, "context_length": 128000},
    {"id": "deepseek/deepseek-chat", "pricing": {"prompt": "0.0002"}, "context_length": 64000},
]


@pytest.fixture
def platform_admin_auth(client):
    User.objects.create_user(
        email="root@momcare.test",
        password="TestPass!2026",
        first_name="Root",
        last_name="Admin",
        role=Role.objects.get(code=settings.ROLE_PLATFORM_ADMIN),
    )
    response = client.post(
        "/api/auth/login/",
        data={"email": "root@momcare.test", "password": "TestPass!2026"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return {"HTTP_AUTHORIZATION": f"Bearer {response.json()['access']}"}


def test_platform_admin_can_read_the_config(client, platform_admin_auth):
    AIProviderConfig.objects.create(current_model="google/gemini-2.0-flash-001", max_words=150)

    response = client.get(CONFIG_URL, **platform_admin_auth)

    assert response.status_code == 200
    assert response.json()["current_model"] == "google/gemini-2.0-flash-001"


def test_patch_with_a_valid_model_succeeds(client, platform_admin_auth):
    AIProviderConfig.objects.create(current_model="google/gemini-2.0-flash-001", max_words=150)

    with patch("momcare_platform.core.ai.openrouter_client.list_available_models", return_value=_CATALOG):
        response = client.patch(
            CONFIG_URL,
            data=json.dumps({"current_model": "deepseek/deepseek-chat"}),
            content_type="application/json",
            **platform_admin_auth,
        )

    assert response.status_code == 200
    assert AIProviderConfig.objects.first().current_model == "deepseek/deepseek-chat"


def test_patch_with_a_model_not_in_the_live_catalog_is_rejected(client, platform_admin_auth):
    AIProviderConfig.objects.create(current_model="google/gemini-2.0-flash-001", max_words=150)

    with patch("momcare_platform.core.ai.openrouter_client.list_available_models", return_value=_CATALOG):
        response = client.patch(
            CONFIG_URL,
            data=json.dumps({"current_model": "totally-made-up/model"}),
            content_type="application/json",
            **platform_admin_auth,
        )

    assert response.status_code == 400
    assert AIProviderConfig.objects.first().current_model == "google/gemini-2.0-flash-001"


def test_a_hospital_admin_is_refused(client, make_hospital, auth):
    hospital = make_hospital("Platform Admin Endpoint Hospital")

    response = client.get(CONFIG_URL, **auth(hospital.admin.email))

    assert response.status_code == 403


def test_available_models_returns_the_live_catalog(client, platform_admin_auth):
    with patch("momcare_platform.core.ai.openrouter_client.list_available_models", return_value=_CATALOG):
        response = client.get(MODELS_URL, **platform_admin_auth)

    assert response.status_code == 200
    assert response.json() == _CATALOG
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest momcare_platform/core/platform_admin/tests/api/test_ai_config.py -v`
Expected: FAIL — 404s (nothing mounted), and `mkdir` is needed first for the test path.

Run before the above: `mkdir -p momcare_platform/core/platform_admin/tests/api && touch momcare_platform/core/platform_admin/tests/__init__.py momcare_platform/core/platform_admin/tests/api/__init__.py` (only if these don't already exist — check first, since `platform_admin/tests/api/__init__.py` was seen to already exist from earlier exploration).

- [ ] **Step 3: Write the serializer**

Replace `momcare_platform/core/platform_admin/api/serializers.py`:

```python
"""Platform-admin API serializers. AIProviderConfig is the first real one --
see api/views.py for why this app existed as an empty skeleton until now."""

from rest_framework import serializers

from momcare_platform.core.ai import openrouter_client
from momcare_platform.core.ai.models import AIProviderConfig


class AIProviderConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIProviderConfig
        fields = ["current_model", "max_words", "custom_instructions"]

    def validate_current_model(self, value):
        # Module-attribute call, not `from ... import list_available_models` --
        # the latter would bind this module's own reference at import time,
        # which a test's `patch("...openrouter_client.list_available_models")`
        # could no longer reach. Same pattern core.ai.services already uses
        # for openrouter_client.generate().
        catalog = openrouter_client.list_available_models()
        if catalog is None:
            # OpenRouter's catalog is briefly unreachable -- fail closed
            # rather than silently accepting a value we can't verify.
            raise serializers.ValidationError("Could not verify this model against OpenRouter right now.")
        valid_ids = {entry["id"] for entry in catalog}
        if value not in valid_ids:
            raise serializers.ValidationError(f"'{value}' is not a model OpenRouter currently serves.")
        return value
```

- [ ] **Step 4: Write the views**

Replace `momcare_platform/core/platform_admin/api/views.py`:

```python
"""Platform-admin API. AI config is the first real capability this app has
had -- previously a documented empty skeleton (see CLAUDE.md)."""

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.ai import openrouter_client
from momcare_platform.core.ai.services import get_ai_config
from momcare_platform.core.common.permissions import IsPlatformAdmin
from momcare_platform.core.platform_admin.api.serializers import AIProviderConfigSerializer


class AIProviderConfigView(APIView):
    """Read/edit the platform-wide model, word cap, and instructions.
    ROLE_PLATFORM_ADMIN only -- not visible to any hospital-side role."""

    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def get(self, request):
        config = get_ai_config()
        return Response(AIProviderConfigSerializer(config).data)

    def patch(self, request):
        config = get_ai_config()
        serializer = AIProviderConfigSerializer(config, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(AIProviderConfigSerializer(config).data)


class AIAvailableModelsView(APIView):
    """The live OpenRouter catalog, for the platform-admin picker UI."""

    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def get(self, request):
        catalog = openrouter_client.list_available_models() or []
        return Response(catalog)
```

- [ ] **Step 5: Mount the URLs**

In `config/api_router.py`, add the import:

```python
from momcare_platform.core.platform_admin.api.views import AIAvailableModelsView, AIProviderConfigView
```

Add to `core_urlpatterns`, replacing the existing trailing comment block that reads:

```python
    # Platform admin API — deferred. core/platform_admin/ exists as an empty
    # skeleton (see CLAUDE.md); no routes until it's actually implemented.
    # Reviewing organizations/deactivation requests goes through Django
    # admin (OrganizationAdmin, OrganizationDeactivationRequestAdmin) until then.
```

with:

```python
    # Platform admin API — AI config is the first real endpoint here (see
    # docs/design/2026-09-27-ai-summary-design.md). Reviewing organizations/
    # deactivation requests still goes through Django admin
    # (OrganizationAdmin, OrganizationDeactivationRequestAdmin) — unaffected.
    re_path(
        r"^platform-admin/ai-config/?$",
        AIProviderConfigView.as_view(),
        name="platform-admin-ai-config",
    ),
    re_path(
        r"^platform-admin/ai-config/available-models/?$",
        AIAvailableModelsView.as_view(),
        name="platform-admin-ai-available-models",
    ),
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest momcare_platform/core/platform_admin/tests/api/test_ai_config.py -v`
Expected: PASS (6 passed)

- [ ] **Step 7: Commit**

```bash
git add momcare_platform/core/platform_admin/ config/api_router.py
git commit -m "feat(platform_admin): add the AI config endpoints, the app's first real feature"
```

---

### Task 13: Organization-level instructions endpoint

**Files:**
- Modify: `momcare_platform/core/organization/api/serializers.py`
- Modify: `momcare_platform/core/organization/api/views.py`
- Modify: `config/api_router.py`
- Test: `momcare_platform/core/organization/tests/api/test_ai_instructions.py`

**Interfaces:**
- Consumes: `Organization.ai_custom_instructions` (Task 3), `IsHospitalAdmin` (`core.common.permissions`) — mirrors `OrganizationConfidenceThresholdView` exactly.
- Produces: URL name `organization-ai-instructions`.

- [ ] **Step 1: Write the failing tests**

`momcare_platform/core/organization/tests/api/test_ai_instructions.py`:

```python
"""PATCH /api/organization/me/ai-instructions/ -- the org-level AI Summary
steering text. hospital_admin only, same shape as confidence-threshold."""

import json

import pytest

pytestmark = pytest.mark.django_db

URL = "/api/organization/me/ai-instructions/"


def test_hospital_admin_can_set_the_instructions(client, make_hospital, auth):
    hospital = make_hospital("AI Instructions Hospital")

    response = client.patch(
        URL,
        data=json.dumps({"ai_custom_instructions": "Always mention medication adherence."}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200
    hospital.org.refresh_from_db()
    assert hospital.org.ai_custom_instructions == "Always mention medication adherence."


def test_a_provider_is_refused(client, make_hospital, make_staff, auth):
    from django.conf import settings

    hospital = make_hospital("AI Instructions Provider Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, email="provider@aiinstructions.test")

    response = client.patch(
        URL,
        data=json.dumps({"ai_custom_instructions": "Should not be allowed."}),
        content_type="application/json",
        **auth(provider.email),
    )

    assert response.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest momcare_platform/core/organization/tests/api/test_ai_instructions.py -v`
Expected: FAIL — 404 (nothing mounted).

- [ ] **Step 3: Add the serializer**

In `momcare_platform/core/organization/api/serializers.py`, add after `OrganizationConfidenceThresholdSerializer`:

```python
class OrganizationAIInstructionsSerializer(serializers.ModelSerializer):
    """This hospital's own free-text AI Summary steering, appended after the
    platform-wide instructions on every generation for this hospital's
    patients only. See docs/design/2026-09-27-ai-summary-design.md."""

    class Meta:
        model = Organization
        fields = ["ai_custom_instructions"]
```

- [ ] **Step 4: Add the view**

In `momcare_platform/core/organization/api/views.py`, add after `OrganizationConfidenceThresholdView` (importing `OrganizationAIInstructionsSerializer` alongside the existing serializer imports at the top of the file):

```python
class OrganizationAIInstructionsView(APIView):
    """Change this hospital's own AI Summary steering text. Only
    hospital_admin may change it — same restriction as
    OrganizationConfidenceThresholdView above, for the same reason: a
    setting that shapes a clinical-facing output shouldn't be editable by
    every staff role."""

    permission_classes = [IsAuthenticated, IsHospitalAdmin]

    def patch(self, request):
        org = request.user.organization
        if org is None:
            return Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)

        serializer = OrganizationAIInstructionsSerializer(org, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(OrganizationSerializer(org, context={"request": request}).data)
```

- [ ] **Step 5: Mount the URL**

In `config/api_router.py`, add `OrganizationAIInstructionsView` to the existing `from momcare_platform.core.organization.api.views import (...)` block, and add to `core_urlpatterns` immediately after the `organization-confidence-threshold` entry:

```python
    re_path(
        r"^organization/me/ai-instructions/?$",
        OrganizationAIInstructionsView.as_view(),
        name="organization-ai-instructions",
    ),
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest momcare_platform/core/organization/tests/api/test_ai_instructions.py -v`
Expected: PASS (2 passed)

- [ ] **Step 7: Commit**

```bash
git add momcare_platform/core/organization/ config/api_router.py
git commit -m "feat(organization): add PATCH /api/organization/me/ai-instructions/"
```

---

### Task 14: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the whole `core` suite**

Run: `uv run pytest momcare_platform/core -q`
Expected: every test passes, including all new `core/ai`, `core/organization`, and `core/platform_admin` tests, with no regression in `core/patients` (Task 10 touched `deactivate_patient()`).

- [ ] **Step 2: Run the import-boundary check**

Run: `uv run lint-imports`
Expected: no violations — `core/ai` never statically imports `modules.pregnancy.vitals` or `modules.pregnancy.alerts`.

- [ ] **Step 3: Run the linter and type checker**

Run: `uv run ruff check .` then `uv run mypy momcare_platform/core/ai momcare_platform/core/organization momcare_platform/core/platform_admin momcare_platform/core/patients`
Expected: clean on both.

- [ ] **Step 4: Confirm `manage.py check` and a migration dry run are clean**

Run: `uv run python manage.py check` then `uv run python manage.py makemigrations --check --dry-run`
Expected: `System check identified no issues (0 silenced).` and no missing migrations reported.

- [ ] **Step 5: Final commit (only if any of the above required a fix)**

```bash
git add -A
git commit -m "chore(ai): fix verification failures from the AI Summary feature"
```

(Skip this step entirely if every check in Steps 1-4 passed with no changes needed.)
