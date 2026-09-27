"""refresh_ai_summaries -- the periodic safety net. Excludes deactivated
patients (Review Focus item); refreshes missing-or-stale summaries only."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import connection
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
    # deactivate_patient() itself now triggers one final generation (Task
    # 10) -- mocked here too, so this test stays network-free regardless of
    # that call succeeding or failing for real.
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Deactivation summary."):
        deactivate_patient(patient, by=hospital.admin)

    with patch("momcare_platform.core.ai.openrouter_client.generate") as mock_generate:
        call_command("refresh_ai_summaries")

    assert not mock_generate.called
    assert AISummary.objects.get(patient=patient).content == "Deactivation summary."


def test_each_patient_gets_its_own_transaction_not_one_sweep_wide_one(make_hospital, settings):
    """Critical review finding: the whole sweep used to run inside a single
    bypass_rls() (itself one transaction.atomic()), row-locking and holding
    open a transaction across every patient's HTTP call for the entire run.
    Each patient must get its own short-lived scope instead."""
    settings.MOMCARE_AI_SUMMARY_REFRESH_HOURS = 4
    hospital = make_hospital("Refresh Per-Patient Hospital")
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        patient_one = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Uzma", "last_name": "Farooq"},
        )
        patient_two = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Sadia", "last_name": "Yousuf"},
        )
    assert not AISummary.objects.filter(patient__in=[patient_one, patient_two]).exists()

    events: list[str] = []

    def query_wrapper(execute, sql, params, many, context):
        if "SET LOCAL app.rls_bypass" in sql:
            events.append("bypass_open")
        return execute(sql, params, many, context)

    def fake_generate(prompt, *, model, max_tokens):
        events.append("client_call")
        return "Refreshed."

    with connection.execute_wrapper(query_wrapper):
        with patch("momcare_platform.core.ai.openrouter_client.generate", side_effect=fake_generate):
            call_command("refresh_ai_summaries")

    assert AISummary.objects.get(patient=patient_one).content == "Refreshed."
    assert AISummary.objects.get(patient=patient_two).content == "Refreshed."

    # One bypass scope to list the due patient ids, then at least a read and
    # a write scope per patient -- never one scope wrapping the whole sweep
    # (which would show as a single "bypass_open" before every client call).
    client_call_indexes = [i for i, e in enumerate(events) if e == "client_call"]
    assert len(client_call_indexes) == 2
    for idx in client_call_indexes:
        assert "bypass_open" in events[:idx]
        assert "bypass_open" in events[idx + 1 :]
    assert events.count("bypass_open") >= 5
