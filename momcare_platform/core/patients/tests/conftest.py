"""Fixtures for the patient-side flow: sign up, verify, fill in her profile."""

import itertools
import json
import re

import pytest
from django.core import mail

from momcare_platform.core.users.models import User

REGISTER = "/api/auth/patient/register/"
VERIFY_EMAIL = "/api/auth/patient/verify-email/"
MY_PROFILE = "/api/my-profile/"

PROFILE_BODY = {
    "date_of_birth": "1996-04-12",
    "blood_group": "O+",
    "national_id": "61101-7654321-0",
    "emergency_contact_name": "Bilal Ahmed",
    "emergency_contact_phone": "03007654321",
    "emergency_contact_relation": "Husband",
    "emergency_contact_email": "bilal@example.test",
}

_phones = itertools.count(1)


def post_json(client, url, body, headers=None):
    return client.post(url, data=json.dumps(body), content_type="application/json", **(headers or {}))


def patch_json(client, url, body, headers=None):
    return client.patch(url, data=json.dumps(body), content_type="application/json", **(headers or {}))


def code_from_email(message):
    """Pull the six-digit OTP out of the emailed body, the way she would read
    it off her own phone."""
    found = re.search(r"\b(\d{6})\b", message.body)
    assert found, "no OTP in the email"
    return found.group(1)


@pytest.fixture
def registered_patient(client):
    """A self-registered, email-verified woman, and her auth headers.

    Goes through the real two-step flow (register, then confirm the OTP
    emailed to her) rather than minting a token directly -- the same
    discipline the ``auth`` fixture in conftest.py already applies to
    hospital logins. Her profile is NOT filled in: that is its own step.
    """

    def _make(email="ayesha@example.test"):
        response = post_json(
            client,
            REGISTER,
            {
                "email": email,
                "password": "HerOwnPick!2026",
                "first_name": "Ayesha",
                "last_name": "Bibi",
                "address_line1": "House 12, Street 4",
                "address_line2": "F-7",
                "city": "Islamabad",
                "state": "ICT",
                "postal_code": "44000",
                "country": "Pakistan",
            },
        )
        assert response.status_code == 201, response.content
        code = code_from_email(mail.outbox[-1])

        verified = post_json(client, VERIFY_EMAIL, {"email": email, "code": code})
        assert verified.status_code == 200, verified.content
        token = verified.json()["access"]
        return User.objects.get(email=email), {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    return _make


@pytest.fixture
def complete_profile(client):
    """Fill in everything the profile step asks for. Each call gets its own
    phone number because ``User.phone`` is unique."""

    def _fill(headers, **overrides):
        body = {"phone": f"0300{next(_phones):07d}", **PROFILE_BODY, **overrides}
        response = patch_json(client, MY_PROFILE, body, headers)
        assert response.status_code == 200, response.content
        return response.json()

    return _fill
