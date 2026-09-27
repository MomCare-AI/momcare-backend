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
        is_email_verified=True,
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
