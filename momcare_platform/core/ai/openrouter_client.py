"""Thin OpenRouter wrapper -- the only place this project makes an outbound
HTTP call to a third-party AI provider.

Best-effort, matching core.common.mail's existing convention: every function
here logs and returns None on any failure rather than raising. A clinician
seeing yesterday's cached AISummary beats seeing an error page.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

import httpx
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_BASE_URL = "https://openrouter.ai/api/v1"
_WEB_MAX_RESULTS = 5
_CATALOG_CACHE_KEY = "ai.openrouter_client.last_known_catalog"


def generate(prompt: str, *, model: str, max_tokens: int, timeout: float = 10.0) -> str | None:
    """One chat-completion call. `model` is always passed in explicitly by
    the caller (read from AIProviderConfig) -- this function has zero
    model-specific branching, which is what makes switching models a
    zero-logic-change operation. ``timeout`` defaults to the short value the
    one-paragraph summary needs; a caller asking for a much longer answer (the
    care-plan generator) passes its own."""
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
            # Kept short (matching list_available_models()'s own timeout) --
            # no lock or transaction is held during this call (see
            # generate_patient_summary()), but a 2-worker gunicorn still ties
            # up one whole worker for as long as this runs.
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception:
        logger.exception("OpenRouter generate() call failed (model=%s)", model)
        return None


class Researched(NamedTuple):
    """An answer plus the web pages the model actually read to write it."""

    text: str
    citations: list[dict]  # [{"title": str, "url": str}], in the order OpenRouter returned them


def generate_researched(
    prompt: str, *, model: str, max_tokens: int, timeout: float = 45.0, web_search: bool = True
) -> Researched | None:
    """Like ``generate`` but with OpenRouter's live web search switched on: the model
    searches the web *for this request* and OpenRouter returns the pages it used as
    ``url_citation`` annotations. The links come from the search, never from the
    model's own words, so they can be opened and checked. ``web_search=False`` is the
    plain call (no citations) used when the search itself is unavailable.

    Best-effort like every function here: logs and returns None on any failure."""
    if not settings.OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY is not set -- skipping generate_researched() call")
        return None
    body: dict = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
    if web_search:
        body["plugins"] = [{"id": "web", "max_results": _WEB_MAX_RESULTS}]
    try:
        response = httpx.post(
            f"{_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
            json=body,
            timeout=timeout,
        )
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        citations = []
        for annotation in message.get("annotations") or []:
            cite = annotation.get("url_citation") if isinstance(annotation, dict) else None
            if isinstance(cite, dict) and cite.get("url"):
                citations.append({"title": str(cite.get("title") or ""), "url": str(cite["url"])})
        return Researched(message["content"], citations)
    except Exception:
        logger.exception("OpenRouter generate_researched() call failed (model=%s, web_search=%s)", model, web_search)
        return None


def list_available_models() -> list[dict] | None:
    """The live model catalog -- used to validate AIProviderConfig.current_model
    on write (see core.platform_admin's config endpoint) and to power the
    platform-admin picker UI. Always fetched fresh, never served from cache
    on a successful call -- the whole point is catching a model that stopped
    being served. Only on failure does it fall back to whatever catalog was
    last fetched successfully (design doc: 'falls back to whatever was
    fetched most recently rather than blocking the page'), rather than
    treating a brief OpenRouter outage as an outright empty catalog.
    """
    try:
        response = httpx.get(f"{_BASE_URL}/models", timeout=10.0)
        response.raise_for_status()
        catalog = [
            {"id": m["id"], "pricing": m.get("pricing"), "context_length": m.get("context_length")}
            for m in response.json()["data"]
        ]
        cache.set(_CATALOG_CACHE_KEY, catalog, timeout=None)
        return catalog
    except Exception:
        logger.exception("OpenRouter list_available_models() call failed")
        return cache.get(_CATALOG_CACHE_KEY)
