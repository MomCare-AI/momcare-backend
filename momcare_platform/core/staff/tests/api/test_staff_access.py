"""Access control around staff onboarding.

Three properties are being defended here:

* **Authority** — only a hospital admin (any location) or a location's own
  manager (only into location(s) they manage) may onboard a new staff member.
* **Isolation** — a hospital can only ever see its own people, and can never
  onboard someone into another hospital's location. This is the one that
  matters most: MomCare is multi-tenant, so a leak here exposes one
  hospital's clinical team to another.
* **Credentials** — the account is created with the password the requester
  set, and that password is emailed to the new staff member so they can
  actually sign in.
"""

import json

import pytest
from django.conf import settings
from django.core import mail

from momcare_platform.core.locations.models import Location
from momcare_platform.core.locations.services import ensure_default_location
from momcare_platform.core.users.models import Role, User

pytestmark = pytest.mark.django_db

STAFF = "/api/staff/"


def _onboard_payload(email="new.doctor@example.test", role_code=settings.ROLE_PROVIDER, locations=(), **extra):
    payload = {
        "email": email,
        "first_name": "New",
        "last_name": "Doctor",
        "role_code": role_code,
        "locations": [str(loc.id) for loc in locations],
    }
    payload.update(extra)
    return payload


def _onboard(client, headers, **kwargs):
    return client.post(
        STAFF,
        data=json.dumps(_onboard_payload(**kwargs)),
        content_type="application/json",
        **headers,
    )


# ── Authority ────────────────────────────────────────────────────────────────


def test_hospital_admin_can_onboard_a_nurse(client, make_hospital, auth):
    hospital = make_hospital("Authority Hospital")
    location = ensure_default_location(hospital.org)
    mail.outbox.clear()

    response = _onboard(
        client,
        auth(hospital.admin.email),
        role_code=settings.ROLE_NURSE,
        locations=[location],
    )

    assert response.status_code == 201, response.content
    assert User.objects.filter(email="new.doctor@example.test", role__code=settings.ROLE_NURSE).exists()
    assert len(mail.outbox) == 1


def test_a_location_manager_can_onboard_staff_into_their_own_location(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Manager Onboard Hospital")
    manager = make_staff(hospital.org, settings.ROLE_NURSE, "manager@manageronboard.test")
    location = Location.objects.create(organization=hospital.org, name="North Wing", location_manager=manager)

    response = _onboard(
        client,
        auth(manager.email),
        role_code=settings.ROLE_CARE_MANAGER,
        locations=[location],
    )

    assert response.status_code == 201, response.content


def test_a_location_manager_cannot_mint_a_new_hospital_admin(client, make_hospital, make_staff, auth):
    """A location manager onboarding role_code=hospital_admin with an empty
    locations list must not slip past both the "locations required" and
    "must manage the location" checks by skipping locations entirely --
    that combination would otherwise mint a brand-new, unrestricted admin
    account for a hospital this person doesn't run."""
    hospital = make_hospital("Escalation Attempt Hospital")
    manager = make_staff(hospital.org, settings.ROLE_NURSE, "manager@escalationattempt.test")
    Location.objects.create(organization=hospital.org, name="Their Branch", location_manager=manager)

    response = _onboard(
        client,
        auth(manager.email),
        role_code=settings.ROLE_HOSPITAL_ADMIN,
        locations=[],
    )

    assert response.status_code == 400
    assert "role_code" in response.json()
    assert not User.objects.filter(email="new.doctor@example.test").exists()


def test_a_location_manager_cannot_onboard_into_a_location_they_dont_manage(
    client,
    make_hospital,
    make_staff,
    auth,
):
    hospital = make_hospital("Manager Overreach Hospital")
    manager = make_staff(hospital.org, settings.ROLE_NURSE, "manager@manageroverreach.test")
    Location.objects.create(organization=hospital.org, name="Their Branch", location_manager=manager)
    someone_elses_location = Location.objects.create(
        organization=hospital.org,
        name="Main Branch",
        location_manager=hospital.admin,
    )

    response = _onboard(
        client,
        auth(manager.email),
        role_code=settings.ROLE_NURSE,
        locations=[someone_elses_location],
    )

    assert response.status_code == 400
    assert "locations" in response.json()


def test_a_staff_member_who_manages_no_location_cannot_onboard_anyone(client, make_hospital, make_staff, auth):
    hospital = make_hospital("No Authority Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doctor@noauthority.test")
    location = ensure_default_location(hospital.org)

    response = _onboard(client, auth(doctor.email), locations=[location])

    assert response.status_code == 403
    assert not User.objects.filter(email="new.doctor@example.test").exists()


def test_provider_can_still_read_the_team(client, make_hospital, make_staff, auth):
    """The restriction is on granting access, not on seeing colleagues."""
    hospital = make_hospital("Readable Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "reader@readable.test")

    response = client.get(STAFF, **auth(doctor.email))

    assert response.status_code == 200
    assert [m["email"] for m in response.json()] == [doctor.email]


# ── Validation ───────────────────────────────────────────────────────────────


def test_admin_cannot_onboard_a_platform_admin(client, make_hospital, auth):
    """No privilege escalation: a hospital admin cannot mint Momcare staff."""
    hospital = make_hospital("Escalation Hospital")

    response = _onboard(client, auth(hospital.admin.email), role_code=settings.ROLE_PLATFORM_ADMIN)

    assert response.status_code == 400
    assert "role_code" in response.json()


def test_admin_cannot_onboard_a_patient_as_staff(client, make_hospital, auth):
    """Patients are enrolled clinically, never onboarded onto the hospital's team."""
    hospital = make_hospital("Patient Role Hospital")

    response = _onboard(client, auth(hospital.admin.email), role_code=settings.ROLE_PATIENT)

    assert response.status_code == 400


def test_a_location_is_required_for_clinical_roles(client, make_hospital, auth):
    hospital = make_hospital("Bare Clinical Hospital")

    response = _onboard(client, auth(hospital.admin.email), role_code=settings.ROLE_NURSE, locations=[])

    assert response.status_code == 400
    assert "locations" in response.json()


def test_a_location_is_optional_for_a_hospital_admin(client, make_hospital, auth):
    hospital = make_hospital("Bare Admin Hospital")

    response = _onboard(client, auth(hospital.admin.email), role_code=settings.ROLE_HOSPITAL_ADMIN, locations=[])

    assert response.status_code == 201, response.content


def test_email_collision_is_rejected(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Collision Hospital")
    existing = make_staff(hospital.org, settings.ROLE_NURSE, "taken@collision.test")
    location = ensure_default_location(hospital.org)

    response = _onboard(client, auth(hospital.admin.email), email=existing.email, locations=[location])

    assert response.status_code == 400
    assert "email" in response.json()


def test_a_location_from_another_hospital_is_rejected(client, make_hospital, auth):
    ours = make_hospital("Ours Onboard Hospital")
    theirs = make_hospital("Theirs Onboard Hospital")
    their_location = ensure_default_location(theirs.org)

    response = _onboard(client, auth(ours.admin.email), role_code=settings.ROLE_NURSE, locations=[their_location])

    assert response.status_code == 400
    assert "locations" in response.json()


# ── Credentials ──────────────────────────────────────────────────────────────


def test_the_invitation_email_carries_a_link_and_never_a_password(client, make_hospital, auth):
    """The account exists but cannot be signed into until the staff member
    follows the link and chooses their own password."""
    hospital = make_hospital("Invitation Hospital")
    location = ensure_default_location(hospital.org)
    mail.outbox.clear()

    response = _onboard(
        client,
        auth(hospital.admin.email),
        email="invited@invitation.test",
        role_code=settings.ROLE_NURSE,
        locations=[location],
    )

    assert response.status_code == 201, response.content
    assert len(mail.outbox) == 1
    body = mail.outbox[0].body
    assert "/reset-password/" in body, "the email must carry a set-password link"
    assert "Password:" not in body, "a password must never be emailed"


def test_an_invited_account_cannot_sign_in_until_activated(client, make_hospital, auth):
    hospital = make_hospital("Not Yet Active Hospital")
    location = ensure_default_location(hospital.org)
    _onboard(
        client,
        auth(hospital.admin.email),
        email="pending@notyetactive.test",
        role_code=settings.ROLE_NURSE,
        locations=[location],
    )

    user = User.objects.get(email="pending@notyetactive.test")

    assert user.requires_password_reset is True
    assert user.has_usable_password() is False


def test_following_the_invitation_link_activates_the_account(client, make_hospital, auth):
    """End to end: invite -> set password -> sign in."""
    hospital = make_hospital("Activate Hospital")
    location = ensure_default_location(hospital.org)
    mail.outbox.clear()
    _onboard(
        client,
        auth(hospital.admin.email),
        email="activating@activate.test",
        role_code=settings.ROLE_NURSE,
        locations=[location],
    )
    # Pull the uid/token straight out of the emailed link, exactly as the
    # frontend would from the URL the staff member clicks.
    link = next(part for part in mail.outbox[0].body.split() if "/reset-password/" in part)
    uid, token = link.rstrip("/").split("/")[-2:]

    reset = client.post(
        "/api/auth/reset-password/",
        data=json.dumps({"uid": uid, "token": token, "new_password": "TheirOwnPick!2026"}),
        content_type="application/json",
    )
    assert reset.status_code == 200, reset.content

    login = client.post(
        "/api/auth/login/",
        data=json.dumps({"email": "activating@activate.test", "password": "TheirOwnPick!2026"}),
        content_type="application/json",
    )
    assert login.status_code == 200, login.content

    user = User.objects.get(email="activating@activate.test")
    assert user.requires_password_reset is False, "activation must clear the flag"


def test_the_invitation_link_cannot_be_used_twice(client, make_hospital, auth):
    """Single-use by construction: the token is derived partly from the
    password hash, so setting a password invalidates the link that set it."""
    hospital = make_hospital("Single Use Hospital")
    location = ensure_default_location(hospital.org)
    mail.outbox.clear()
    _onboard(
        client,
        auth(hospital.admin.email),
        email="once@singleuse.test",
        role_code=settings.ROLE_NURSE,
        locations=[location],
    )
    link = next(part for part in mail.outbox[0].body.split() if "/reset-password/" in part)
    uid, token = link.rstrip("/").split("/")[-2:]
    payload = {"uid": uid, "token": token, "new_password": "FirstChoice!2026"}
    first = client.post("/api/auth/reset-password/", data=json.dumps(payload), content_type="application/json")
    assert first.status_code == 200

    second = client.post(
        "/api/auth/reset-password/",
        data=json.dumps({**payload, "new_password": "SecondChoice!2026"}),
        content_type="application/json",
    )

    assert second.status_code == 400


def test_the_staff_list_shows_who_has_not_activated_yet(client, make_hospital, auth):
    hospital = make_hospital("Activation Visible Hospital")
    location = ensure_default_location(hospital.org)
    _onboard(
        client,
        auth(hospital.admin.email),
        email="pendingrow@activationvisible.test",
        role_code=settings.ROLE_NURSE,
        locations=[location],
    )

    rows = client.get("/api/staff/", **auth(hospital.admin.email)).json()
    invited = next(r for r in rows if r["email"] == "pendingrow@activationvisible.test")

    assert invited["has_activated"] is False


def test_an_admin_cannot_set_a_staff_password(client, make_hospital, make_staff, auth):
    """Link-only by design: if an admin could set it, "this clinician
    acknowledged the alert" would not be provable."""
    hospital = make_hospital("No Admin Password Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@noadminpassword.test")

    response = client.patch(
        f"/api/staff/{nurse.staff.id}/",
        data=json.dumps({"password": "AdminChosen!2026"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200, response.content
    login = client.post(
        "/api/auth/login/",
        data=json.dumps({"email": nurse.email, "password": "AdminChosen!2026"}),
        content_type="application/json",
    )
    assert login.status_code != 200, "the password field must be ignored, not applied"


# ── Isolation — the property the whole tenancy model rests on ────────────────


def test_staff_list_never_leaks_another_hospital(client, make_hospital, make_staff, auth):
    """Hospital A must not see Hospital B's clinical team."""
    alpha = make_hospital("Alpha Hospital")
    beta = make_hospital("Beta Hospital")
    make_staff(alpha.org, settings.ROLE_PROVIDER, "alpha.doc@alpha.test")
    make_staff(beta.org, settings.ROLE_PROVIDER, "beta.doc@beta.test")

    alpha_emails = {m["email"] for m in client.get(STAFF, **auth(alpha.admin.email)).json()}
    beta_emails = {m["email"] for m in client.get(STAFF, **auth(beta.admin.email)).json()}

    assert "alpha.doc@alpha.test" in alpha_emails
    assert "beta.doc@beta.test" in beta_emails
    assert not alpha_emails & beta_emails, "staff lists leaked across hospitals"


def test_platform_admin_gets_no_cross_tenant_staff_list(client, make_hospital, make_staff, auth):
    """The scoping mixin treats platform admins as unrestricted, which is right
    for admin tooling and wrong for a hospital portal. They have no hospital, so
    these endpoints must refuse rather than return every hospital's team."""
    alpha = make_hospital("Alpha Platform")
    make_staff(alpha.org, settings.ROLE_PROVIDER, "alpha.doc@platform.test")

    platform_admin = User.objects.create_user(
        email="platform@momcare.test",
        password="TestPass!2026",
        first_name="Platform",
        last_name="Admin",
        role=Role.objects.get(code=settings.ROLE_PLATFORM_ADMIN),
    )

    response = client.get(STAFF, **auth(platform_admin.email))

    assert response.status_code == 404
    assert "not attached to a hospital" in response.json()["detail"]


def test_organization_endpoint_returns_only_your_own_hospital(client, make_hospital, auth):
    alpha = make_hospital("Alpha Org")
    make_hospital("Beta Org")

    response = client.get("/api/organization/me/", **auth(alpha.admin.email))

    assert response.status_code == 200
    assert response.json()["name"] == "Alpha Org"
