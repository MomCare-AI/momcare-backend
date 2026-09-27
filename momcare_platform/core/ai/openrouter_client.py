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
    if not settings.OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY is not set -- skipping generate() call")
        return None
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
