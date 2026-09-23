"""Signing in.

Email is the only accepted identifier, deliberately. Phone sign-in was built
and then removed: a phone number has no single global format, so the same
number arrives as "+923001234567", "0300 1234567" or "03001234567" depending
on who typed it, and only the exact spelling stored at signup would ever
authenticate. See ``LoginView``'s docstring for the full reasoning.

``User.phone`` still exists and is still collected — it is contact
information, not a credential.
"""

import json

import pytest

LOGIN = "/api/auth/login/"
PASSWORD = "HerOwnPick!2026"


def login(client, **body):
    return client.post(LOGIN, data=json.dumps(body), content_type="application/json")


@pytest.fixture
def account(make_hospital):
    """One approved hospital's admin, with a known password."""
    hospital = make_hospital("Login Hospital")
    user = hospital.admin
    user.set_password(PASSWORD)
    user.save(update_fields=["password"])
    return user


# ── Signing in ───────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_sign_in_with_email(client, account):
    response = login(client, email=account.email, password=PASSWORD)

    assert response.status_code == 200
    assert response.json()["user"]["email"] == account.email


@pytest.mark.django_db
def test_email_is_case_insensitive(client, account):
    """She typed her address with a capital; that is the same account."""
    response = login(client, email=account.email.upper(), password=PASSWORD)

    assert response.status_code == 200


@pytest.mark.django_db
def test_surrounding_whitespace_is_ignored(client, account):
    """Phone keyboards and copy-paste both add trailing spaces."""
    response = login(client, email=f"  {account.email}  ", password=PASSWORD)

    assert response.status_code == 200


# ── Failures ─────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_a_wrong_password_is_rejected(client, account):
    response = login(client, email=account.email, password="WrongOne!2026")

    assert response.status_code == 401


@pytest.mark.django_db
def test_an_unknown_email_is_rejected(client, account):
    response = login(client, email="nobody@example.test", password=PASSWORD)

    assert response.status_code == 401


@pytest.mark.django_db
def test_an_unknown_email_answers_exactly_like_a_wrong_password(client, account):
    """Neither response may reveal whether an account exists.

    A different status or message for "no such address" would turn this
    endpoint into a way to ask which women are registered here, which for a
    maternity platform is itself sensitive.
    """
    unknown = login(client, email="nobody@example.test", password=PASSWORD)
    wrong = login(client, email=account.email, password="WrongOne!2026")

    assert unknown.status_code == wrong.status_code
    assert unknown.json() == wrong.json()


@pytest.mark.django_db
def test_a_missing_email_is_a_400(client, account):
    response = login(client, password=PASSWORD)

    assert response.status_code == 400


@pytest.mark.django_db
def test_a_missing_password_is_a_400(client, account):
    response = login(client, email=account.email)

    assert response.status_code == 400


@pytest.mark.django_db
def test_an_inactive_account_cannot_sign_in(client, account):
    """A deactivated account is refused — as 401, not 403.

    Django's ModelBackend checks ``is_active`` itself and returns None, so an
    inactive user is indistinguishable here from a wrong password. That makes
    ``LoginView``'s own ``if not user.is_active`` branch unreachable; it is
    kept as a guard in case the authentication backend is ever swapped for
    one that does not perform the check.

    401 is also the safer answer: "this account is disabled" would confirm
    the address belongs to someone.
    """
    account.is_active = False
    account.save(update_fields=["is_active"])

    assert login(client, email=account.email, password=PASSWORD).status_code == 401


@pytest.mark.django_db
def test_a_phone_number_is_not_a_way_in(client, account):
    """Phone sign-in is gone on purpose — a number in the email field is
    simply an unknown address, not a fallback lookup."""
    account.phone = "+923001234567"
    account.save(update_fields=["phone"])

    response = login(client, email="+923001234567", password=PASSWORD)

    assert response.status_code == 401
