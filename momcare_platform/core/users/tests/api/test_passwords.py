"""Changing and resetting a password.

Before this existed, a clinician who forgot their password was locked out of
their own account permanently - their hospital admin had no way to help, and
there was no way to change a password that had been shared or guessed either.

These care about two things beyond the happy path: that a reset cannot be used
to discover who holds an account, and that changing a password actually ends
the sessions that used the old one.
"""

import json
import re

import pytest
from django.conf import settings
from django.core import mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from momcare_platform.core.users.models import User

pytestmark = pytest.mark.django_db

CHANGE = "/api/auth/password/change/"
RESET = "/api/auth/password/reset/"
CONFIRM = "/api/auth/password/reset/confirm/"
LOGIN = "/api/auth/login/"

NEW = "A-Completely-New-Pass!2026"


def post(client, url, payload, **headers):
    return client.post(url, data=json.dumps(payload), content_type="application/json", **headers)


def link_parts(message):
    """Pull uid and token out of the emailed URL, the way a recipient's browser would."""
    found = re.search(r"/reset-password/([^/\s]+)/([^\s]+)", message.body)
    assert found, "no reset link in the email"
    return found.group(1), found.group(2)


# -- Changing it while signed in ---------------------------------------------


def test_a_user_can_change_their_own_password(client, make_hospital, auth):
    hospital = make_hospital("Change Hospital")

    response = post(
        client,
        CHANGE,
        {"current_password": hospital.password, "new_password": NEW},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200, response.content
    hospital.admin.refresh_from_db()
    assert hospital.admin.check_password(NEW)


def test_the_current_password_is_required_even_when_signed_in(client, make_hospital, auth):
    """An access token proves the session was authenticated once, not that the
    person at the keyboard owns it. An unlocked screen is otherwise enough to
    take an account over."""
    hospital = make_hospital("Proof Hospital")

    response = post(
        client,
        CHANGE,
        {"current_password": "not-the-right-one", "new_password": NEW},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400
    hospital.admin.refresh_from_db()
    assert hospital.admin.check_password(hospital.password)


def test_a_weak_new_password_is_refused(client, make_hospital, auth):
    hospital = make_hospital("Weak Hospital")

    response = post(
        client,
        CHANGE,
        {"current_password": hospital.password, "new_password": "12345678"},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400
    hospital.admin.refresh_from_db()
    assert hospital.admin.check_password(hospital.password)


def test_the_new_password_must_differ_from_the_old(client, make_hospital, auth):
    hospital = make_hospital("Same Hospital")

    response = post(
        client,
        CHANGE,
        {"current_password": hospital.password, "new_password": hospital.password},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400


def test_changing_a_password_ends_the_sessions_that_used_the_old_one(client, make_hospital, auth):
    """This is the point of changing a password.

    A password is changed because somebody else may have it. Leaving refresh
    tokens valid would let an intruder keep their session for up to a week while
    the owner believes they have just locked the door.

    The stolen cookie is put back deliberately after the change. The endpoint
    also clears it in the browser that made the request, and relying on that
    would test tidiness rather than revocation - an intruder holds their own
    copy and would never have sent the delete.
    """
    hospital = make_hospital("Revoke Hospital")

    login = post(client, LOGIN, {"email": hospital.admin.email, "password": hospital.password})
    assert login.status_code == 200
    stolen = client.cookies.get(settings.REFRESH_COOKIE_NAME)
    assert stolen is not None
    stolen_value = stolen.value

    changed = post(
        client,
        CHANGE,
        {"current_password": hospital.password, "new_password": NEW},
        **auth(hospital.admin.email),
    )
    assert changed.status_code == 200

    client.cookies[settings.REFRESH_COOKIE_NAME] = stolen_value
    refreshed = client.post("/api/auth/refresh/")

    assert refreshed.status_code in (400, 401, 403), (
        "the refresh token issued before the change still works"
    )
    assert b"blacklist" in refreshed.content.lower(), refreshed.content


def test_an_anonymous_caller_cannot_change_a_password(client):
    response = post(client, CHANGE, {"current_password": "x", "new_password": NEW})
    assert response.status_code in (401, 403)


# -- Resetting it when you cannot sign in ------------------------------------


def test_a_reset_link_arrives_and_sets_a_new_password(client, make_hospital):
    hospital = make_hospital("Reset Hospital")
    mail.outbox.clear()

    asked = post(client, RESET, {"email": hospital.admin.email})
    assert asked.status_code == 200
    assert len(mail.outbox) == 1

    uid, token = link_parts(mail.outbox[0])
    done = post(client, CONFIRM, {"uid": uid, "token": token, "new_password": NEW})
    assert done.status_code == 200, done.content

    hospital.admin.refresh_from_db()
    assert hospital.admin.check_password(NEW)


def test_an_unknown_address_is_answered_exactly_like_a_known_one(client, make_hospital):
    """Otherwise this endpoint tells anyone who works at which hospital.

    For a clinical system that list is worth having, and it is reachable without
    signing in, so the two responses must be indistinguishable.
    """
    hospital = make_hospital("Quiet Hospital")
    mail.outbox.clear()

    known = post(client, RESET, {"email": hospital.admin.email})
    unknown = post(client, RESET, {"email": "nobody@nowhere.test"})

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert len(mail.outbox) == 1


def test_a_reset_link_works_once(client, make_hospital):
    """Django's token is derived from the password hash, so using it changes the
    password and invalidates the token in the same step - single-use without a
    table of live tokens sitting in the database waiting to be stolen."""
    hospital = make_hospital("Once Hospital")
    mail.outbox.clear()
    post(client, RESET, {"email": hospital.admin.email})
    uid, token = link_parts(mail.outbox[0])

    first = post(client, CONFIRM, {"uid": uid, "token": token, "new_password": NEW})
    assert first.status_code == 200

    second = post(
        client,
        CONFIRM,
        {"uid": uid, "token": token, "new_password": "Another-One!2026"},
    )
    assert second.status_code == 400

    hospital.admin.refresh_from_db()
    assert hospital.admin.check_password(NEW)


def test_a_tampered_token_is_refused(client, make_hospital):
    hospital = make_hospital("Tamper Hospital")
    mail.outbox.clear()
    post(client, RESET, {"email": hospital.admin.email})
    uid, token = link_parts(mail.outbox[0])
    broken = token[:-1] + ("a" if token[-1] != "a" else "b")

    response = post(client, CONFIRM, {"uid": uid, "token": broken, "new_password": NEW})

    assert response.status_code == 400
    hospital.admin.refresh_from_db()
    assert hospital.admin.check_password(hospital.password)


def test_a_reset_cannot_be_pointed_at_another_account(client, make_hospital):
    """The uid names the account and the token is derived from it, so a valid
    token for one user cannot be replayed against another."""
    victim = make_hospital("Victim Hospital")
    attacker = make_hospital("Attacker Hospital")
    mail.outbox.clear()

    post(client, RESET, {"email": attacker.admin.email})
    _, token = link_parts(mail.outbox[0])
    victim_uid = urlsafe_base64_encode(force_bytes(victim.admin.pk))

    response = post(client, CONFIRM, {"uid": victim_uid, "token": token, "new_password": NEW})

    assert response.status_code == 400
    victim.admin.refresh_from_db()
    assert victim.admin.check_password(victim.password)


def test_a_weak_password_is_refused_on_reset_too(client, make_hospital):
    hospital = make_hospital("Weak Reset Hospital")
    mail.outbox.clear()
    post(client, RESET, {"email": hospital.admin.email})
    uid, token = link_parts(mail.outbox[0])

    response = post(client, CONFIRM, {"uid": uid, "token": token, "new_password": "password"})

    assert response.status_code == 400
    hospital.admin.refresh_from_db()
    assert hospital.admin.check_password(hospital.password)


def test_a_deactivated_account_gets_no_reset_link(client, make_hospital):
    """Deactivation is how access is withdrawn. A reset link would hand it back."""
    hospital = make_hospital("Closed Hospital")
    User.objects.filter(pk=hospital.admin.pk).update(is_active=False)
    mail.outbox.clear()

    response = post(client, RESET, {"email": hospital.admin.email})

    assert response.status_code == 200
    assert len(mail.outbox) == 0


def test_the_reset_email_names_no_detail_beyond_the_address(client, make_hospital):
    """Anyone can type an address into a reset form, so the message may reach
    somebody who does not hold the account."""
    hospital = make_hospital("Discreet Hospital")
    mail.outbox.clear()
    post(client, RESET, {"email": hospital.admin.email})

    body = mail.outbox[0].body
    assert "Discreet Hospital" not in body
    assert "was not you" in body.lower()
