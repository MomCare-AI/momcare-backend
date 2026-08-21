"""Access control around staff and invitations.

Two properties are being defended here:

* **Authority** — only a hospital admin may grant access, and never at a higher
  level than their own.
* **Isolation** — a hospital can only ever see its own people. This is the one
  that matters most: MomCare is multi-tenant, so a leak here exposes one
  hospital's clinical team to another.
"""

import pytest
from django.conf import settings

from momcare_platform.core.staff.models import StaffInvite
from momcare_platform.core.users.models import Role, User

pytestmark = pytest.mark.django_db

STAFF = "/api/staff/"
INVITES = "/api/staff/invites/"


def _invite_payload(email="new.doctor@example.test", role_code=settings.ROLE_PROVIDER):
    return {"email": email, "first_name": "New", "last_name": "Doctor", "role_code": role_code}


def _create_invite(client, headers, **kwargs):
    return client.post(
        INVITES,
        data=_invite_payload(**kwargs),
        content_type="application/json",
        **headers,
    )


# ── Authority ────────────────────────────────────────────────────────────────


def test_provider_cannot_create_an_invite(client, make_hospital, make_staff, auth):
    """Clinical staff can see the team but must not be able to grow it."""
    hospital = make_hospital("Authority Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doctor@authority.test")

    response = _create_invite(client, auth(doctor.email))

    assert response.status_code == 403
    assert not StaffInvite.objects.filter(email="new.doctor@example.test").exists()


def test_provider_can_still_read_the_team(client, make_hospital, make_staff, auth):
    """The restriction is on granting access, not on seeing colleagues."""
    hospital = make_hospital("Readable Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "reader@readable.test")

    response = client.get(STAFF, **auth(doctor.email))

    assert response.status_code == 200
    assert [m["email"] for m in response.json()] == [doctor.email]


def test_admin_cannot_invite_a_platform_admin(client, make_hospital, auth):
    """No privilege escalation: a hospital admin cannot mint Momcare staff."""
    hospital = make_hospital("Escalation Hospital")

    response = _create_invite(
        client,
        auth(hospital.admin.email),
        role_code=settings.ROLE_PLATFORM_ADMIN,
    )

    assert response.status_code == 400
    assert "role_code" in response.json()


def test_admin_cannot_invite_a_patient_as_staff(client, make_hospital, auth):
    """Patients are enrolled clinically, never invited onto the hospital's team."""
    hospital = make_hospital("Patient Role Hospital")

    response = _create_invite(client, auth(hospital.admin.email), role_code=settings.ROLE_PATIENT)

    assert response.status_code == 400


# ── Invitation lifecycle ─────────────────────────────────────────────────────


def test_accepted_invite_cannot_be_reused(client, make_hospital, auth):
    """An invitation link is single-use — a leaked link must not mint a second account."""
    hospital = make_hospital("Replay Hospital")
    token = _create_invite(client, auth(hospital.admin.email)).json()["token"]

    accept_url = f"/api/invites/{token}/accept/"
    first = client.post(
        accept_url,
        data={"first_name": "New", "last_name": "Doctor", "password": "FirstPass!2026"},
        content_type="application/json",
    )
    assert first.status_code == 201

    second = client.post(
        accept_url,
        data={"first_name": "Imposter", "last_name": "X", "password": "SecondPass!2026"},
        content_type="application/json",
    )

    assert second.status_code == 400
    assert "already been used" in second.json()["detail"].lower()
    assert User.objects.filter(email="new.doctor@example.test").count() == 1


def test_accepting_cannot_choose_its_own_hospital_or_role(client, make_hospital, auth):
    """Tenant and role come from the invite row, never from the acceptance request.

    Even when the recipient submits their own organization and role, the created
    user lands in the inviting hospital at the invited role.
    """
    inviting = make_hospital("Inviting Hospital")
    other = make_hospital("Other Hospital")
    token = _create_invite(client, auth(inviting.admin.email)).json()["token"]

    response = client.post(
        f"/api/invites/{token}/accept/",
        data={
            "first_name": "New",
            "last_name": "Doctor",
            "password": "ChosenPass!2026",
            # Attacker-supplied fields — the serializer must ignore them.
            "organization": str(other.org.id),
            "role_code": settings.ROLE_HOSPITAL_ADMIN,
        },
        content_type="application/json",
    )
    assert response.status_code == 201

    created = User.objects.get(email="new.doctor@example.test")
    assert created.organization_id == inviting.org.id
    assert created.role_code == settings.ROLE_PROVIDER


def test_revoked_invite_cannot_be_accepted(client, make_hospital, auth):
    hospital = make_hospital("Revoke Hospital")
    headers = auth(hospital.admin.email)
    invite = _create_invite(client, headers).json()

    revoked = client.post(f"{INVITES}{invite['id']}/revoke/", **headers)
    assert revoked.status_code == 200

    response = client.post(
        f"/api/invites/{invite['token']}/accept/",
        data={"first_name": "New", "last_name": "Doctor", "password": "RevokedPass!2026"},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert not User.objects.filter(email="new.doctor@example.test").exists()


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


def test_invite_list_never_leaks_another_hospital(client, make_hospital, auth):
    alpha = make_hospital("Alpha Invites")
    beta = make_hospital("Beta Invites")
    _create_invite(client, auth(alpha.admin.email), email="pending.alpha@alpha.test")
    _create_invite(client, auth(beta.admin.email), email="pending.beta@beta.test")

    alpha_emails = {i["email"] for i in client.get(INVITES, **auth(alpha.admin.email)).json()}

    assert alpha_emails == {"pending.alpha@alpha.test"}


def test_admin_cannot_revoke_another_hospitals_invite(client, make_hospital, auth):
    """Scoping must hold on writes too, not just on list views."""
    alpha = make_hospital("Alpha Revoke")
    beta = make_hospital("Beta Revoke")
    beta_invite = _create_invite(client, auth(beta.admin.email), email="beta.pending@beta.test").json()

    response = client.post(f"{INVITES}{beta_invite['id']}/revoke/", **auth(alpha.admin.email))

    assert response.status_code == 404, "one hospital must not touch another's invitations"
    assert StaffInvite.objects.get(id=beta_invite["id"]).revoked_at is None


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
