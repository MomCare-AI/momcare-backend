"""Best-effort OpenRouter client -- mocked throughout, never a real network
call in this suite, same posture as core.common.mail's own tests."""

from unittest.mock import patch

import httpx

from momcare_platform.core.ai.openrouter_client import Researched, generate, generate_researched, list_available_models


def _mock_response(json_data, status_code=200):
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return httpx.Response(status_code, json=json_data, request=request)


def test_generate_returns_the_model_text_on_success(settings):
    settings.OPENROUTER_API_KEY = "test-key"
    payload = {"choices": [{"message": {"content": "A short summary."}}]}
    with patch("httpx.post", return_value=_mock_response(payload)):
        result = generate("some prompt", model="google/gemini-2.0-flash-001", max_tokens=400)

    assert result == "A short summary."


def test_generate_returns_none_on_http_error_and_never_raises(settings):
    settings.OPENROUTER_API_KEY = "test-key"
    with patch("httpx.post", side_effect=httpx.ConnectTimeout("timed out")):
        result = generate("some prompt", model="google/gemini-2.0-flash-001", max_tokens=400)

    assert result is None


def test_generate_returns_none_on_malformed_response(settings):
    settings.OPENROUTER_API_KEY = "test-key"
    with patch("httpx.post", return_value=_mock_response({"unexpected": "shape"})):
        result = generate("some prompt", model="google/gemini-2.0-flash-001", max_tokens=400)

    assert result is None


def test_generate_short_circuits_when_no_api_key_is_configured(settings):
    settings.OPENROUTER_API_KEY = ""
    with patch("httpx.post") as mock_post:
        result = generate("some prompt", model="google/gemini-2.0-flash-001", max_tokens=400)

    assert result is None
    assert not mock_post.called


def test_generate_researched_turns_the_web_plugin_on_and_returns_the_pages_it_used(settings):
    settings.OPENROUTER_API_KEY = "test-key"
    payload = {
        "choices": [
            {
                "message": {
                    "content": "{}",
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url_citation": {"title": "Guide", "url": "https://moh.gov.pk/g.pdf"},
                        },
                        {"type": "url_citation", "url_citation": {"title": "No link", "url": ""}},
                        {"type": "something_else"},
                    ],
                }
            }
        ]
    }
    with patch("httpx.post", return_value=_mock_response(payload)) as post:
        result = generate_researched("p", model="m", max_tokens=100)

    assert result == Researched("{}", [{"title": "Guide", "url": "https://moh.gov.pk/g.pdf"}])
    assert post.call_args.kwargs["json"]["plugins"] == [{"id": "web", "max_results": 5}]


def test_generate_researched_without_web_search_sends_no_plugin_and_has_no_citations(settings):
    settings.OPENROUTER_API_KEY = "test-key"
    payload = {"choices": [{"message": {"content": "text"}}]}
    with patch("httpx.post", return_value=_mock_response(payload)) as post:
        result = generate_researched("p", model="m", max_tokens=100, web_search=False)

    assert result == Researched("text", [])
    assert "plugins" not in post.call_args.kwargs["json"]


def test_generate_researched_returns_none_on_failure_and_never_raises(settings):
    settings.OPENROUTER_API_KEY = "test-key"
    with patch("httpx.post", side_effect=httpx.ConnectTimeout("timed out")):
        assert generate_researched("p", model="m", max_tokens=100) is None
    with patch("httpx.post", return_value=_mock_response({"unexpected": "shape"})):
        assert generate_researched("p", model="m", max_tokens=100) is None


def test_generate_researched_short_circuits_without_an_api_key(settings):
    settings.OPENROUTER_API_KEY = ""
    with patch("httpx.post") as post:
        assert generate_researched("p", model="m", max_tokens=100) is None
    assert not post.called


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


def test_list_available_models_falls_back_to_the_last_successful_catalog(settings):
    """Design doc: 'If the catalog is briefly unreachable, the endpoint falls
    back to whatever was fetched most recently rather than blocking the
    page.' The client had no such fallback -- every failure returned None
    regardless of a previous success."""
    from django.core.cache import cache

    cache.clear()
    payload = {
        "data": [{"id": "google/gemini-2.0-flash-001", "pricing": {"prompt": "0.0001"}, "context_length": 128000}],
    }
    with patch("httpx.get", return_value=_mock_response(payload)):
        first_call = list_available_models()
    assert first_call is not None

    with patch("httpx.get", side_effect=httpx.ConnectTimeout("timed out")):
        second_call = list_available_models()

    assert second_call == first_call
