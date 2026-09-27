"""Proves the autouse safety net in momcare_platform/conftest.py: creating a
patient WITHOUT explicitly mocking openrouter_client must never attempt a
real network call, even though the enrollment trigger fires unconditionally
on every Patient creation. Found by code review: every test in the whole
suite that creates a patient (not just this app's own tests) was previously
exposed to this."""

from unittest.mock import patch

import pytest

from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db


def test_creating_a_patient_with_no_explicit_mock_never_calls_httpx(make_hospital):
    hospital = make_hospital("No Real Network Hospital")

    # openrouter_client.generate() is best-effort and swallows any exception
    # (including one raised by a mock), so a raising side_effect can't prove
    # this -- it would pass either way. Assert on .called instead.
    with patch("httpx.post") as mock_post:
        # No mock of openrouter_client.generate here -- this is the point.
        onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": "Real", "last_name": "Network"},
        )

    assert not mock_post.called
