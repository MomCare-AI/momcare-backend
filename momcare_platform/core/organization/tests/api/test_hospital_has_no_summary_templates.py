"""Summary templates are platform-only (2026-10-01): a hospital admin has no
template endpoints at all, not even read access."""

import pytest

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "enhance/",
        "00000000-0000-0000-0000-000000000000/activate/",
        "00000000-0000-0000-0000-000000000000/deactivate/",
    ],
)
def test_hospital_admin_template_endpoints_are_gone(client, make_hospital, auth, suffix):
    hospital = make_hospital("No Hospital Templates Hospital")
    url = f"/api/organization/me/summary-templates/{suffix}"

    assert client.get(url, **auth(hospital.admin.email)).status_code == 404
    assert client.post(url, data={}, content_type="application/json", **auth(hospital.admin.email)).status_code == 404
