"""Sending mail over HTTPS instead of SMTP.

Managed hosts commonly block outbound SMTP to stop their platform being used
for spam — Railway is one of them, and the symptom is a connection that simply
times out rather than an error naming the cause. Every credential can be
correct and nothing will ever arrive.

Port 443 is not blocked anywhere, because blocking it would break the platform
itself. So this backend speaks Resend's HTTP API over the same port the app
already uses to reach its database.

Written against ``urllib`` rather than the ``resend`` SDK deliberately: this is
one POST with a JSON body, and a dependency added three weeks before submission
is a dependency nobody has time to audit.

Configure with::

    DJANGO_EMAIL_BACKEND=momcare_platform.core.common.email_backends.ResendHTTPEmailBackend

The API key is read from ``DJANGO_RESEND_API_KEY``, falling back to
``DJANGO_EMAIL_HOST_PASSWORD`` so a deployment already configured for SMTP
switches over without touching its secrets.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)

API_URL = "https://api.resend.com/emails"
TIMEOUT = 10


class ResendHTTPEmailBackend(BaseEmailBackend):
    """Delivers each message as one POST to Resend.

    No connection is held open between sends. There is nothing to reuse — the
    volume here is invitations and alerts, not a campaign — and a pooled
    connection would only add a failure mode that appears under load.
    """

    def __init__(self, fail_silently: bool = False, **kwargs) -> None:
        super().__init__(fail_silently=fail_silently)
        self.api_key = getattr(settings, "RESEND_API_KEY", "") or settings.EMAIL_HOST_PASSWORD

    def send_messages(self, email_messages) -> int:
        if not email_messages:
            return 0
        if not self.api_key:
            # Loud, because the alternative is invitations silently vanishing.
            logger.error("No Resend API key configured; %d message(s) not sent.", len(email_messages))
            if not self.fail_silently:
                raise ValueError("Resend API key is not configured.")
            return 0

        return sum(1 for message in email_messages if self._send(message))

    def _send(self, message) -> bool:
        recipients = list(message.to or [])
        if not recipients:
            return False

        payload = {
            "from": message.from_email or settings.DEFAULT_FROM_EMAIL,
            "to": recipients,
            "subject": message.subject,
            "text": message.body,
        }
        if message.cc:
            payload["cc"] = list(message.cc)
        if message.bcc:
            payload["bcc"] = list(message.bcc)
        if message.reply_to:
            payload["reply_to"] = list(message.reply_to)

        # The project sends plain text by design, but honour an HTML alternative
        # if a caller ever attaches one rather than dropping it silently.
        for content, mimetype in getattr(message, "alternatives", []) or []:
            if mimetype == "text/html":
                payload["html"] = content
                break

        request = urllib.request.Request(  # noqa: S310 - constant https URL
            API_URL,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
                logger.info("Resend accepted %r for %s (%s)", message.subject, recipients, response.status)
        except urllib.error.HTTPError as exc:
            # Resend explains refusals in the body - an unverified sending
            # domain, a malformed address - and that explanation is the whole
            # value of the log line.
            detail = exc.read().decode(errors="replace")[:400]
            logger.error("Resend refused %r for %s: %s %s", message.subject, recipients, exc.code, detail)
            if not self.fail_silently:
                raise
            return False
        except Exception:
            logger.exception("Could not reach Resend to send %r to %s", message.subject, recipients)
            if not self.fail_silently:
                raise
            return False
        return True
