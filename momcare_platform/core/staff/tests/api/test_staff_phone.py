"""A staff member's phone number.

Contact information, not a credential — sign-in is by email only (see
``LoginView``). A hospital holds it so it can reach a clinician about an
alert away from the portal.

The trap these tests exist to hold down: ``User.phone`` is ``unique=True``,
so storing "" for "no number given" would make the *second* staff member
without one collide with the first. Blank must become NULL, and any number
of NULLs coexist.
"""

import json

import pytest
from django.conf import settings

from momcare_platform.core.locations.models import Location
from momcare_platform.core.staff.models import Staff
from momcare_platform.core.users.models import User

pytestmark = pytest.mark.django_db

STAFF_URL = "/api/staff/"


def detail_url(staff_id):
    return f"/api/staff/{staff_id}/"


def post(client, headers, url, **fields):
    return client.post(url, data=json.dumps(fields), content_type="application/json", **headers)


def patch(client, headers, url, **fields):
    return client.patch(url, data=json.dumps(fields), content_type="application/json", **headers)


def invite(client, headers, hospital, email, **extra):
    """Invite a nurse. Clinical roles must be assigned at least one location,
    so reuse the hospital's branch or create it on first call."""
    location = Location.objects.filter(organization=hospital.org).first() or Location.objects.create(
        organization=hospital.org,
        name="Main Branch",
    )
    return post(
        client,
        headers,
        STAFF_URL,
        email=email,
        role_code=settings.ROLE_NURSE,
        locations=[str(location.id)],
        **extra,
    )


# ── Onboarding ───────────────────────────────────────────────────────────────


def test_a_phone_number_is_stored_at_onboarding(client, make_hospital, auth):
    hospital = make_hospital("Phone Intake Hospital")

    response = invite(
        client,
        auth(hospital.admin.email),
        hospital,
        "nurse@phoneintake.test",
        first_name="Ayesha",
        phone="+923001234567",
    )

    assert response.status_code == 201, response.content
    assert User.objects.get(email="nurse@phoneintake.test").phone == "+923001234567"


def test_the_phone_number_is_returned_on_the_staff_list(client, make_hospital, auth):
    """An admin must be able to read back what they entered, or they cannot
    tell a missing number from one that failed to save."""
    hospital = make_hospital("Phone Readback Hospital")
    headers = auth(hospital.admin.email)
    invite(client, headers, hospital, "nurse@phonereadback.test", phone="+923009876543")

    body = client.get(STAFF_URL, **headers).json()
    # The staff list is unpaginated (a hospital's team is small); other list
    # endpoints return the {count, results} envelope, so accept either rather
    # than encoding that difference into this test.
    listed = body["results"] if isinstance(body, dict) else body
    nurse = next(s for s in listed if s["email"] == "nurse@phonereadback.test")

    assert nurse["phone"] == "+923009876543"


def test_a_phone_number_is_optional(client, make_hospital, auth):
    hospital = make_hospital("Phone Optional Hospital")

    response = invite(client, auth(hospital.admin.email), hospital, "nurse@phoneoptional.test")

    assert response.status_code == 201, response.content


def test_two_staff_without_a_phone_can_both_be_onboarded(client, make_hospital, auth):
    """The unique-constraint trap: "" for both would be a duplicate; NULL is not.

    Without the blank-to-NULL conversion this is an IntegrityError 500 on the
    second invite — and every hospital onboards more than one person.
    """
    hospital = make_hospital("Two Blank Phones Hospital")
    headers = auth(hospital.admin.email)

    first = invite(client, headers, hospital, "one@twoblank.test")
    second = invite(client, headers, hospital, "two@twoblank.test")

    assert first.status_code == 201, first.content
    assert second.status_code == 201, second.content
    assert User.objects.filter(email__endswith="@twoblank.test", phone__isnull=True).count() == 2


def test_a_phone_already_in_use_is_a_400_not_a_500(client, make_hospital, auth):
    hospital = make_hospital("Duplicate Phone Hospital")
    headers = auth(hospital.admin.email)
    invite(client, headers, hospital, "first@dupphone.test", phone="+923001112222")

    response = invite(client, headers, hospital, "second@dupphone.test", phone="+923001112222")

    assert response.status_code == 400, response.content
    assert "phone" in json.dumps(response.json()).lower()


def test_a_phone_held_at_another_hospital_is_still_rejected(client, make_hospital, auth):
    """User.phone is unique platform-wide, so the check cannot be scoped to
    the onboarding hospital or it becomes an IntegrityError instead of a 400."""
    alpha = make_hospital("Alpha Phone Hospital")
    beta = make_hospital("Beta Phone Hospital")
    invite(client, auth(alpha.admin.email), alpha, "nurse@alphaphone.test", phone="+923004445555")

    response = invite(client, auth(beta.admin.email), beta, "nurse@betaphone.test", phone="+923004445555")

    assert response.status_code == 400, response.content


# ── Updating ─────────────────────────────────────────────────────────────────


def test_an_admin_can_change_a_staff_members_phone(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Phone Update Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@phoneupdate.test")

    response = patch(
        client,
        auth(hospital.admin.email),
        detail_url(nurse.staff.id),
        phone="+923007778888",
    )

    assert response.status_code == 200, response.content
    nurse.refresh_from_db()
    assert nurse.phone == "+923007778888"


def test_clearing_a_phone_stores_null_not_empty_string(client, make_hospital, make_staff, auth):
    """Same unique-constraint trap as onboarding, on the update path."""
    hospital = make_hospital("Phone Clear Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@phoneclear.test")
    patch(client, auth(hospital.admin.email), detail_url(nurse.staff.id), phone="+923006665555")

    response = patch(client, auth(hospital.admin.email), detail_url(nurse.staff.id), phone="")

    assert response.status_code == 200, response.content
    nurse.refresh_from_db()
    assert nurse.phone is None, "an empty string would collide with the next blank phone"


def test_resubmitting_someones_own_unchanged_phone_is_allowed(client, make_hospital, make_staff, auth):
    """A PATCH that resends the current number must not be rejected as a
    duplicate of the person themselves."""
    hospital = make_hospital("Phone Resubmit Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@phoneresubmit.test")
    headers = auth(hospital.admin.email)
    patch(client, headers, detail_url(nurse.staff.id), phone="+923002223333")

    response = patch(client, headers, detail_url(nurse.staff.id), phone="+923002223333", first_name="Ayesha")

    assert response.status_code == 200, response.content


def test_taking_a_phone_already_held_by_someone_else_is_a_400(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Phone Steal Hospital")
    headers = auth(hospital.admin.email)
    invite(client, headers, hospital, "holder@phonesteal.test", phone="+923009990000")
    other = make_staff(hospital.org, settings.ROLE_NURSE, "other@phonesteal.test")

    response = patch(client, headers, detail_url(other.staff.id), phone="+923009990000")

    assert response.status_code == 400, response.content


# ── Not a credential ─────────────────────────────────────────────────────────


def test_a_staff_phone_is_not_a_way_to_sign_in(client, make_hospital, auth):
    """Phone login was deliberately removed — no global format means the
    stored spelling is the only one that would ever match."""
    hospital = make_hospital("Phone Not Login Hospital")
    invite(client, auth(hospital.admin.email), hospital, "nurse@phonenotlogin.test", phone="+923005556666")
    user = User.objects.get(email="nurse@phonenotlogin.test")
    user.set_password("HerOwnPick!2026")
    user.save(update_fields=["password"])

    response = client.post(
        "/api/auth/login/",
        data=json.dumps({"email": "+923005556666", "password": "HerOwnPick!2026"}),
        content_type="application/json",
    )

    assert response.status_code == 401


def test_onboarding_still_accepts_no_password(client, make_hospital, auth):
    """Adding phone must not have opened a door to admin-set passwords —
    that is what makes alert acknowledgement provable."""
    hospital = make_hospital("Phone No Password Hospital")

    invite(
        client,
        auth(hospital.admin.email),
        hospital,
        "nurse@phonenopassword.test",
        phone="+923008889999",
        password="AdminChose!2026",
    )

    user = User.objects.get(email="nurse@phonenopassword.test")
    assert not user.has_usable_password()
    assert user.requires_password_reset
    assert Staff.objects.filter(user=user).exists()
