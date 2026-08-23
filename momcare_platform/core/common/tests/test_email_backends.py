"""The HTTPS email backend.

These matter because the failure this backend exists to fix was invisible: SMTP
timed out on the host, mail.py logged and swallowed it, and the portal reported
an invitation as created. Nothing anywhere said "no email was sent".
"""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest
from django.core.mail import EmailMessage, EmailMultiAlternatives

from momcare_platform.core.common.email_backends import ResendHTTPEmailBackend


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _message(**kwargs) -> EmailMessage:
    defaults = {
        "subject": "Invitation",
        "body": "Open the link.",
        "from_email": "MomCare <noreply@momcare.solutions>",
        "to": ["doctor@example.com"],
    }
    return EmailMessage(**{**defaults, **kwargs})


def _captured(request) -> dict:
    return json.loads(request.data.decode())


@pytest.fixture
def backend(settings):
    settings.RESEND_API_KEY = "re_test_key"
    return ResendHTTPEmailBackend()


def test_posts_to_resend_and_reports_one_sent(backend):
    with patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
        assert backend.send_messages([_message()]) == 1

    request = urlopen.call_args[0][0]
    assert request.full_url == "https://api.resend.com/emails"
    assert request.get_header("Authorization") == "Bearer re_test_key"
    body = _captured(request)
    assert body["to"] == ["doctor@example.com"]
    assert body["subject"] == "Invitation"
    assert body["text"] == "Open the link."


def test_uses_port_443_not_smtp(backend):
    """The whole point. If this ever became an smtp:// URL the fix is undone."""
    with patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
        backend.send_messages([_message()])
    assert urlopen.call_args[0][0].full_url.startswith("https://")


def test_carries_cc_bcc_and_reply_to(backend):
    message = _message(cc=["ward@example.com"], bcc=["audit@example.com"])
    message.reply_to = ["admin@example.com"]
    with patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
        backend.send_messages([message])
    body = _captured(urlopen.call_args[0][0])
    assert body["cc"] == ["ward@example.com"]
    assert body["bcc"] == ["audit@example.com"]
    assert body["reply_to"] == ["admin@example.com"]


def test_includes_an_html_alternative_rather_than_dropping_it(backend):
    message = EmailMultiAlternatives(
        subject="Invitation",
        body="Open the link.",
        from_email="MomCare <noreply@momcare.solutions>",
        to=["doctor@example.com"],
    )
    message.attach_alternative("<p>Open the link.</p>", "text/html")
    with patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
        backend.send_messages([message])
    assert _captured(urlopen.call_args[0][0])["html"] == "<p>Open the link.</p>"


def test_a_message_with_no_recipient_is_not_sent(backend):
    with patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
        assert backend.send_messages([_message(to=[])]) == 0
    urlopen.assert_not_called()


def test_missing_api_key_raises_rather_than_silently_dropping(settings):
    settings.RESEND_API_KEY = ""
    settings.EMAIL_HOST_PASSWORD = ""
    with pytest.raises(ValueError, match="not configured"):
        ResendHTTPEmailBackend().send_messages([_message()])


def test_falls_back_to_the_smtp_password_so_no_new_secret_is_needed(settings):
    settings.RESEND_API_KEY = ""
    settings.EMAIL_HOST_PASSWORD = "re_from_smtp_var"
    assert ResendHTTPEmailBackend().api_key == "re_from_smtp_var"


def test_a_refusal_is_reported_not_swallowed(backend):
    refusal = urllib.error.HTTPError(
        "https://api.resend.com/emails", 403, "Forbidden", {}, BytesIO(b'{"message":"domain not verified"}')
    )
    with patch("urllib.request.urlopen", side_effect=refusal), pytest.raises(urllib.error.HTTPError):
        backend.send_messages([_message()])


def test_fail_silently_returns_zero_instead_of_raising(settings):
    settings.RESEND_API_KEY = "re_test_key"
    quiet = ResendHTTPEmailBackend(fail_silently=True)
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        assert quiet.send_messages([_message()]) == 0


def test_mail_helper_still_reports_failure_through_this_backend(settings):
    """mail.py promises never to raise. That promise must survive the new transport."""
    from momcare_platform.core.common import mail

    settings.EMAIL_BACKEND = "momcare_platform.core.common.email_backends.ResendHTTPEmailBackend"
    settings.RESEND_API_KEY = "re_test_key"
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        assert mail._send("Subject", "Body", "doctor@example.com") is False
