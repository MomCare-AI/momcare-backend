"""The model region a hospital's patients belong to, end to end.

Region is never asked for during onboarding — it is derived from the country the
hospital already gives. These check that it reaches the API, that a pregnancy
inherits it through the tenancy path, and that a hospital outside the model's
training says so rather than being handed a population it does not belong to.
"""

import json

import pytest

from momcare_platform.core.common import regions
from momcare_platform.core.organization.models import Organization
from momcare_platform.core.patients.models import Patient

pytestmark = pytest.mark.django_db

ORGANIZATION = "/api/organization/me/"
PATIENTS = "/api/patients/"

REGISTRATION = {
    "first_name": "Bilal",
    "last_name": "Ahmed",
    "email": "owner@sunrise.test",
    "password": "BrandNewPass!2026",
    "org_name": "Sunrise Maternity",
    "org_email": "info@sunrise.test",
    "org_phone": "0511111111",
    "address_line1": "22 Blue Area",
    "city": "Islamabad",
    "state": "ICT",
    "postal_code": "44000",
    "country": "Pakistan",
    "license_no": "IHRA-2026-115",
    "license_authority": "ihra",
}


def _register(client, **overrides):
    payload = {**REGISTRATION, **overrides}
    response = client.post(
        "/api/auth/register/",
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    return Organization.objects.get(name=payload["org_name"])


def _set_country(hospital, country):
    hospital.org.country = country
    hospital.org.save(update_fields=["country", "updated_at"])


# ── Derivation ───────────────────────────────────────────────────────────────


def test_onboarding_asks_for_no_region_and_still_knows_it(client):
    """The whole design in one test.

    Nothing in the registration payload mentions a region, and the hospital
    still ends up with the right one — because it comes from the country that
    was already required.
    """
    assert not any("region" in key for key in REGISTRATION)

    organization = _register(client)

    assert organization.region == regions.REGION_ASIA
    assert organization.region_display == "Asia"


def test_a_hospital_outside_the_training_data_says_so(client):
    """Nothing is invented for a population the model has never seen."""
    organization = _register(client, country="Germany", org_name="Berlin Mitte Klinik")

    assert organization.region is None
    assert organization.region_display == "Outside supported regions"


def test_correcting_the_country_moves_the_region_with_it(client):
    """Derived, not stored — which is why there is no second field to forget.

    A country entered wrongly and corrected later must not leave a stale region
    behind, feeding the model the wrong population indefinitely.
    """
    organization = _register(client, country="Germany", org_name="Relocated General")
    assert organization.region is None

    organization.country = "Nigeria"
    organization.save(update_fields=["country", "updated_at"])
    organization.refresh_from_db()

    assert organization.region == regions.REGION_AFRICA


# ── Reaching a pregnancy ─────────────────────────────────────────────────────


def test_a_pregnancy_inherits_the_region_from_its_hospital(client, make_hospital, auth):
    """Reached through the patient's location, the same path every other
    tenant-owned lookup takes — so there is no second place it can be set."""
    hospital = make_hospital("Nur Care")
    _set_country(hospital, "Pakistan")

    response = client.post(
        PATIENTS,
        data=json.dumps(
            {
                "first_name": "Ayesha",
                "last_name": "Bibi",
                "date_of_birth": "1998-04-11",
                "consent": {"status": "granted", "version": "v1.0", "method": "in_person"},
            },
        ),
        content_type="application/json",
        **auth(hospital.admin.email),
    )
    assert response.status_code == 201, response.content

    patient = Patient.objects.get(id=response.json()["id"])
    assert patient.organization.region == regions.REGION_ASIA


# ── Over the API ─────────────────────────────────────────────────────────────


def test_the_api_reports_the_region(client, make_hospital, auth):
    hospital = make_hospital("Kenya Care")
    _set_country(hospital, "Kenya")

    response = client.get(ORGANIZATION, **auth(hospital.admin.email))
    assert response.status_code == 200, response.content

    body = response.json()
    assert body["region"] == regions.REGION_AFRICA
    assert body["region_display"] == "Africa"


def test_the_api_reports_null_rather_than_omitting_the_key(client, make_hospital, auth):
    """An absent key reads as "not implemented"; null reads as "no model for this"."""
    hospital = make_hospital("Stockholm Neo")
    _set_country(hospital, "Sweden")

    body = client.get(ORGANIZATION, **auth(hospital.admin.email)).json()

    assert "region" in body
    assert body["region"] is None
    assert body["region_display"] == "Outside supported regions"


def test_region_is_read_only_over_the_api(client, make_hospital, auth):
    """It is a consequence of the country, not a value a hospital can set.

    Allowing it to be posted would recreate exactly the contradiction this
    design exists to prevent — a hospital in Pakistan filed under Africa.
    """
    hospital = make_hospital("Read Only Care")
    _set_country(hospital, "Pakistan")
    headers = auth(hospital.admin.email)

    response = client.patch(
        ORGANIZATION,
        data=json.dumps({"region": regions.REGION_AFRICA}),
        content_type="application/json",
        **headers,
    )
    assert response.status_code in (200, 403, 405), response.content

    hospital.org.refresh_from_db()
    assert hospital.org.region == regions.REGION_ASIA
