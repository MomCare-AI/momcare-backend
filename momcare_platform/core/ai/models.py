from django.db import models

from momcare_platform.core.common.models import TimeStampedModel, UUIDPrimaryKeyModel


class AIProviderConfig(UUIDPrimaryKeyModel, TimeStampedModel):
    """Platform-wide AI Summary configuration -- exactly one row, read via
    ``services.get_ai_config()`` and never any other way. Editable at runtime
    by the platform admin (see core.platform_admin's new endpoint) -- model
    choice and word cap are cost/infra levers kept platform-only. Custom
    instruction text lives in ``AIInstructionPreset`` instead (a history of
    named, activatable presets, not a single mutable field) -- see
    docs/design/2026-09-28-ai-instruction-presets-design.md.
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

    def __str__(self) -> str:
        return f"AI summary for {self.patient_id}"


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


class AISummaryTemplate(UUIDPrimaryKeyModel, TimeStampedModel):
    """A named, historical arrangement of the AI Summary's fixed data fields
    into ordered, labeled sections -- organization=None is platform-wide, a
    set organization is that hospital's own. Same lifecycle as
    AIInstructionPreset: immutable once created, never deleted, at most one
    active per scope.

    Unlike AIInstructionPreset, this never carries free-form prompt text --
    ``sections`` is structured data (an ordered list of {"label", "fields"}),
    and ``fields`` may only reference the fixed vocabulary
    core.ai.services._TEMPLATE_FIELD_VOCABULARY. A template can rearrange
    which existing fields appear where; it can never introduce a fact that
    isn't already collected, and it can never touch the base prompt's fixed
    safety rules (never invent a value, state gaps plainly, the word cap,
    the closing recommendation) -- those stay in code, never in a template.
    See docs/design/2026-09-28-ai-summary-templates-design.md.
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
