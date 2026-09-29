from django.db import models

from momcare_platform.core.common.models import TimeStampedModel, UUIDPrimaryKeyModel


class AIProviderConfig(UUIDPrimaryKeyModel, TimeStampedModel):
    """Platform-wide AI Summary configuration -- exactly one row, read via
    ``services.get_ai_config()`` and never any other way. Editable at runtime
    by the platform admin (see core.platform_admin's new endpoint) -- model
    choice and word cap are cost/infra levers kept platform-only. Custom
    instruction text lives on the active ``AISummaryTemplate`` instead (its
    ``extra_instructions`` field) -- see
    docs/design/2026-09-29-ai-summary-template-merge-design.md.
    """

    # Standard Django singleton pattern: every row is forced to the same
    # value, so a unique constraint on it makes a second row impossible at
    # the database level (a race that two concurrent first-callers of
    # get_ai_config() could otherwise both win, each creating their own row).
    singleton_id = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    current_model = models.CharField(
        max_length=200,
        help_text="An OpenRouter model id, e.g. 'google/gemini-2.0-flash-001'.",
    )
    max_words = models.PositiveIntegerField(default=150)

    def __str__(self) -> str:
        return f"AI config ({self.current_model})"


class AISummary(UUIDPrimaryKeyModel, TimeStampedModel):
    """One cached AI-generated narrative per patient."""

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
    # Computed once, at generation time, by services._build_citations() from
    # the exact data snapshot that built this row's own content -- never
    # recomputed later against the patient's current (possibly since-changed)
    # data, which could drift from what this specific piece of text actually
    # says. Each entry is {"text", "type" ("reading"|"staff"), "id"} -- the
    # frontend's job to render as a link; this row never claims a link is
    # right, only that the cited record is real. Empty list, never null.
    citations = models.JSONField(default=list, blank=True)

    def __str__(self) -> str:
        return f"AI summary for {self.patient_id}"


class AISummaryTemplate(UUIDPrimaryKeyModel, TimeStampedModel):
    """A named, historical arrangement of the AI Summary's fixed data fields
    into ordered, labeled sections, plus optional extra wording -- organization=None
    is platform-wide, a set organization is that hospital's own. Immutable once
    created, never deleted, at most one active per scope.

    ``sections`` is structured data (an ordered list of {"label", "fields"}), and
    ``fields`` may only reference the fixed vocabulary
    core.ai.services.TEMPLATE_FIELD_VOCABULARY. A template can rearrange which
    existing fields appear where; it can never introduce a fact that isn't already
    collected, and it can never touch the base prompt's fixed safety rules (never
    invent a value, state gaps plainly, the word cap, the closing recommendation)
    -- those stay in code, never in a template.

    ``extra_instructions`` (added 2026-09-29, merged in from the retired
    AIInstructionPreset -- see
    docs/design/2026-09-29-ai-summary-template-merge-design.md) is free-form text
    appended to the prompt, same job a preset used to do, now saved and activated
    together with the layout as one unit rather than as a separate resource. Unlike
    the old preset system (which stacked platform + org text together), this does
    NOT stack -- the active template (layout AND wording) is picked by the same
    org-then-platform-then-default precedence _resolve_active_template() already
    uses, never merged across tiers.
    """

    organization = models.ForeignKey(
        "organization.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="ai_summary_templates",
    )
    name = models.CharField(max_length=200)
    sections = models.JSONField()
    extra_instructions = models.TextField(blank=True)
    is_active = models.BooleanField(default=False)
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
        verbose_name = "AI Summary Template"
        verbose_name_plural = "AI Summary Templates"

    def __str__(self) -> str:
        return self.name
