"""Model regions, derived from country.

The mapping itself is data, so most of these guard the *edges*: the countries
outside the model's training, the blank country, and the promise that every
country a hospital can actually choose gets a defined answer.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from momcare_platform.core.common import regions

FORM = Path("../frontend/src/features/hospital-onboarding/components/HwOrgStep3Location.tsx")


@pytest.mark.parametrize(
    ("country", "expected"),
    [
        ("Pakistan", regions.REGION_ASIA),
        ("India", regions.REGION_ASIA),
        ("Bangladesh", regions.REGION_ASIA),
        ("Nigeria", regions.REGION_AFRICA),
        ("Kenya", regions.REGION_AFRICA),
        ("South Africa", regions.REGION_AFRICA),
        ("Brazil", regions.REGION_AMERICAS),
        ("United States", regions.REGION_AMERICAS),
        ("Mexico", regions.REGION_AMERICAS),
    ],
)
def test_countries_map_to_their_region(country, expected):
    assert regions.region_for_country(country) == expected


@pytest.mark.parametrize("country", ["Austria", "Australia", "Germany", "New Zealand", "Norway"])
def test_untrained_populations_return_none_rather_than_a_guess(country):
    """The whole point of the null.

    Folding Austria into Asia to avoid a null would hand the model a population
    it has never seen, and the output would look exactly like a confident
    answer. None means "fall back to the rules", which is the honest behaviour.
    """
    assert regions.region_for_country(country) is None


@pytest.mark.parametrize("value", ["", None, "   ", "Narnia", "PAKISTAN"])
def test_nothing_unrecognised_becomes_a_region(value):
    """Including case: the form supplies exact names, so a near-miss is a miss.

    Guessing at "PAKISTAN" would invite guessing at "Pak" and "pakistan " next,
    and a fuzzy match that is wrong is worse here than no match at all.
    """
    assert regions.region_for_country(value) is None


def test_surrounding_whitespace_is_forgiven():
    """A trailing space from a paste should not cost a patient her region."""
    assert regions.region_for_country("  Pakistan  ") == regions.REGION_ASIA


def test_every_supported_region_is_reachable():
    """Guards against a mapping where one region has quietly lost all its rows."""
    produced = set(regions._BY_COUNTRY.values())
    assert produced == set(regions.SUPPORTED_REGIONS)


def test_labels_exist_for_every_region_and_for_none():
    for region in regions.SUPPORTED_REGIONS:
        assert regions.region_label(region) not in ("", None)
    assert regions.region_label(None) == "Outside supported regions"


def test_is_supported_agrees_with_the_lookup():
    assert regions.is_supported("Pakistan") is True
    assert regions.is_supported("Austria") is False
    assert regions.is_supported(None) is False


@pytest.mark.skipif(not FORM.exists(), reason="frontend repo not checked out beside backend")
def test_every_country_the_form_offers_is_a_decision_somebody_made():
    """The form and this mapping must not drift apart.

    A country a hospital can select but this module has never heard of returns
    None — identical at runtime to the countries we deliberately do not
    support. That is the drift this catches: adding a country to the dropdown
    fails here until it is either mapped to a region or named as out of scope.
    """
    source = FORM.read_text(encoding="utf-8")
    block = re.search(r"const COUNTRIES = \[(.*?)\];", source, re.S)
    assert block, "could not find the COUNTRIES list in the onboarding form"

    offered = set(re.findall(r'"([^"]+)"', block.group(1)))
    assert len(offered) > 50, "parsed too few countries - the form's shape has changed"

    undecided = offered - set(regions._BY_COUNTRY) - regions._OUT_OF_SCOPE
    assert not undecided, (
        "these countries can be selected during onboarding but have no region "
        f"decision: {sorted(undecided)}"
    )


@pytest.mark.skipif(not FORM.exists(), reason="frontend repo not checked out beside backend")
def test_the_mapping_never_names_a_country_the_form_cannot_offer():
    """A row for a country nobody can pick is dead weight, and usually a typo."""
    source = FORM.read_text(encoding="utf-8")
    block = re.search(r"const COUNTRIES = \[(.*?)\];", source, re.S)
    offered = set(re.findall(r'"([^"]+)"', block.group(1)))

    stale = (set(regions._BY_COUNTRY) | regions._OUT_OF_SCOPE) - offered
    assert not stale, f"named here but not offered by the form: {sorted(stale)}"


def test_a_country_is_never_both_mapped_and_out_of_scope():
    assert not set(regions._BY_COUNTRY) & regions._OUT_OF_SCOPE


@pytest.mark.parametrize("country", sorted(regions._OUT_OF_SCOPE)[:6])
def test_out_of_scope_countries_resolve_to_none(country):
    assert regions.region_for_country(country) is None


# ── The frontend copy ────────────────────────────────────────────────────────
#
# The onboarding form shows the region as soon as a country is picked, which
# needs the mapping in the browser. That copy is display-only — the server
# always derives the stored value — but a copy that can drift is exactly the
# contradiction this whole design exists to prevent, so it is asserted here.

FRONTEND_MAP = Path("../frontend/src/features/hospital-onboarding/regions.ts")


def _parse_typescript_map() -> dict[str, str]:
    source = FRONTEND_MAP.read_text(encoding="utf-8")
    block = re.search(r"COUNTRY_REGION: Record<string, Region> = \{(.*?)\n\};", source, re.S)
    assert block, "could not find COUNTRY_REGION in the frontend module"

    pairs = re.findall(r'\n\s*(?:"([^"]+)"|([A-Za-z]+)):\s*"(asia|africa|americas)"', block.group(1))
    return {quoted or bare: region for quoted, bare, region in pairs}


@pytest.mark.skipif(not FRONTEND_MAP.exists(), reason="frontend repo not checked out beside backend")
def test_the_frontend_mapping_matches_this_one_exactly():
    """One country disagreeing is the whole failure mode.

    The form would tell a hospital in Nigeria it is in Asia while the server
    files it under Africa — and the reassuring thing on screen would be the
    wrong one.
    """
    theirs = _parse_typescript_map()
    ours = dict(regions._BY_COUNTRY)

    assert len(theirs) > 50, "parsed too few entries - the frontend file's shape has changed"

    missing = sorted(set(ours) - set(theirs))
    extra = sorted(set(theirs) - set(ours))
    disagree = sorted(c for c in set(ours) & set(theirs) if ours[c] != theirs[c])

    assert not missing, f"in Python but not in the form: {missing}"
    assert not extra, f"in the form but not in Python: {extra}"
    assert not disagree, f"mapped to different regions: {disagree}"


@pytest.mark.skipif(not FRONTEND_MAP.exists(), reason="frontend repo not checked out beside backend")
def test_the_frontend_uses_the_same_wording_for_an_unsupported_country():
    """The label a hospital reads must match the one the API returns."""
    source = FRONTEND_MAP.read_text(encoding="utf-8")
    assert f'"{regions.region_label(None)}"' in source
