"""Best-effort OpenRouter client -- mocked throughout, never a real network
call in this suite, same posture as core.common.mail's own tests."""

from unittest.mock import patch

import httpx

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
