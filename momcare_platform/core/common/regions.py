"""Which population a hospital's patients belong to.

The risk model is trained per region, because maternal baselines are not
universal - anaemia prevalence, body habitus, the age distribution of first
pregnancy and the background rate of hypertensive disorders all differ enough
between populations that one model applied everywhere is worse than three
applied where they fit.

**Derived from the country already recorded on the organization, never stored.**
Onboarding asks for a country and nothing else. A second question would be a
second answer that can contradict the first - a hospital in Lahore filed under
Africa - and the wrong one would silently reach the model as a population it
was not trained on.

The same reasoning as ``obstetrics.calculate_gestational_age``: one function,
one home, corrections flow through automatically.

Framework-free and database-free, so it tests in milliseconds and the mapping
is readable in one place.
"""

from __future__ import annotations

REGION_ASIA = "asia"
REGION_AFRICA = "africa"
REGION_AMERICAS = "americas"

#: Regions the model has been trained on. Order is not meaningful.
SUPPORTED_REGIONS = (REGION_ASIA, REGION_AFRICA, REGION_AMERICAS)

REGION_LABELS = {
    REGION_ASIA: "Asia",
    REGION_AFRICA: "Africa",
    REGION_AMERICAS: "Americas",
}

# Keyed on the exact country names the onboarding form offers, so a country a
# hospital can choose is always a country this can answer for. Anything absent
# is deliberately absent - see the note at the bottom.
_BY_COUNTRY = {
    # Asia
    "Afghanistan": REGION_ASIA,
    "Armenia": REGION_ASIA,
    "Azerbaijan": REGION_ASIA,
    "Bahrain": REGION_ASIA,
    "Bangladesh": REGION_ASIA,
    "Cambodia": REGION_ASIA,
    "China": REGION_ASIA,
    "Cyprus": REGION_ASIA,
    "Georgia": REGION_ASIA,
    "India": REGION_ASIA,
    "Indonesia": REGION_ASIA,
    "Iran": REGION_ASIA,
    "Iraq": REGION_ASIA,
    "Israel": REGION_ASIA,
    "Japan": REGION_ASIA,
    "Jordan": REGION_ASIA,
    "Kazakhstan": REGION_ASIA,
    "Kuwait": REGION_ASIA,
    "Kyrgyzstan": REGION_ASIA,
    "Lebanon": REGION_ASIA,
    "Malaysia": REGION_ASIA,
    "Myanmar": REGION_ASIA,
    "Nepal": REGION_ASIA,
    "Oman": REGION_ASIA,
    "Pakistan": REGION_ASIA,
    "Palestine": REGION_ASIA,
    "Philippines": REGION_ASIA,
    "Qatar": REGION_ASIA,
    "Saudi Arabia": REGION_ASIA,
    "Singapore": REGION_ASIA,
    "South Korea": REGION_ASIA,
    "Sri Lanka": REGION_ASIA,
    "Syria": REGION_ASIA,
    "Taiwan": REGION_ASIA,
    "Tajikistan": REGION_ASIA,
    "Thailand": REGION_ASIA,
    "Turkey": REGION_ASIA,
    "Turkmenistan": REGION_ASIA,
    "United Arab Emirates": REGION_ASIA,
    "Uzbekistan": REGION_ASIA,
    "Vietnam": REGION_ASIA,
    "Yemen": REGION_ASIA,
    # Africa
    "Algeria": REGION_AFRICA,
    "Cameroon": REGION_AFRICA,
    "Congo (DRC)": REGION_AFRICA,
    "Egypt": REGION_AFRICA,
    "Ethiopia": REGION_AFRICA,
    "Ghana": REGION_AFRICA,
    "Kenya": REGION_AFRICA,
    "Libya": REGION_AFRICA,
    "Mali": REGION_AFRICA,
    "Morocco": REGION_AFRICA,
    "Mozambique": REGION_AFRICA,
    "Nigeria": REGION_AFRICA,
    "Senegal": REGION_AFRICA,
    "Somalia": REGION_AFRICA,
    "South Africa": REGION_AFRICA,
    "Sudan": REGION_AFRICA,
    "Tanzania": REGION_AFRICA,
    "Tunisia": REGION_AFRICA,
    "Uganda": REGION_AFRICA,
    "Zambia": REGION_AFRICA,
    "Zimbabwe": REGION_AFRICA,
    # Americas
    "Argentina": REGION_AMERICAS,
    "Bolivia": REGION_AMERICAS,
    "Brazil": REGION_AMERICAS,
    "Canada": REGION_AMERICAS,
    "Chile": REGION_AMERICAS,
    "Colombia": REGION_AMERICAS,
    "Costa Rica": REGION_AMERICAS,
    "Cuba": REGION_AMERICAS,
    "Dominican Republic": REGION_AMERICAS,
    "Ecuador": REGION_AMERICAS,
    "El Salvador": REGION_AMERICAS,
    "Guatemala": REGION_AMERICAS,
    "Honduras": REGION_AMERICAS,
    "Jamaica": REGION_AMERICAS,
    "Mexico": REGION_AMERICAS,
    "Nicaragua": REGION_AMERICAS,
    "Panama": REGION_AMERICAS,
    "Paraguay": REGION_AMERICAS,
    "Peru": REGION_AMERICAS,
    "United States": REGION_AMERICAS,
    "Uruguay": REGION_AMERICAS,
    "Venezuela": REGION_AMERICAS,
}

#: Countries the onboarding form offers that the model has **no** region for.
#:
#: Listed rather than simply omitted so that every country a hospital can pick
#: is a decision somebody made. An unmapped country and a deliberately
#: unsupported one both return None at runtime and are indistinguishable there;
#: naming them here is what lets a test tell the difference, and what makes
#: adding a country to the dropdown fail until its region is decided.
#:
#: Folding Austria into Asia to avoid a null would hand the model a population
#: it has never seen, and the output would look exactly like a confident
#: answer. None means "fall back to the clinical rules".
_OUT_OF_SCOPE = frozenset(
    {
        # Europe
        "Albania",
        "Austria",
        "Belarus",
        "Belgium",
        "Bosnia and Herzegovina",
        "Bulgaria",
        "Croatia",
        "Czech Republic",
        "Denmark",
        "Estonia",
        "Finland",
        "France",
        "Germany",
        "Greece",
        "Hungary",
        "Ireland",
        "Italy",
        "Latvia",
        "Lithuania",
        "Luxembourg",
        "Moldova",
        "Netherlands",
        "Norway",
        "Poland",
        "Portugal",
        "Romania",
        "Russia",
        "Serbia",
        "Slovakia",
        "Slovenia",
        "Spain",
        "Sweden",
        "Switzerland",
        "Ukraine",
        "United Kingdom",
        # Oceania
        "Australia",
        "New Zealand",
    },
)


def region_for_country(country: str | None) -> str | None:
    """The model region for a country name, or None when there is no model for it.

    None is a real answer, not a failure: it means "this population is outside
    what the model was trained on", and the caller is expected to fall back
    rather than guess.
    """
    if not country:
        return None
    return _BY_COUNTRY.get(country.strip())


def region_label(region: str | None) -> str:
    """Human-readable, for an interface. Unsupported regions say so plainly."""
    return REGION_LABELS.get(region or "", "Outside supported regions")


def is_supported(country: str | None) -> bool:
    return region_for_country(country) is not None
