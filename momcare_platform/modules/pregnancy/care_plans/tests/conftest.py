"""Fixtures for the care plan tests.

Real Postgres, the real trained risk model and the real signal chain -- only
the OpenRouter call is replaced, and only by a fake that returns whatever JSON
the test asks for and counts how often it was asked.
"""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.conf import settings
from django.utils import timezone

from momcare_platform.core.ai.openrouter_client import Researched
from momcare_platform.core.patients.services import onboard_patient
from momcare_platform.core.users.models import Role, User
from momcare_platform.modules.pregnancy.vitals.models import VitalReading

# Vitals picked from the trained model's own tests (vitals/tests/test_reassess_risk.py)
# so each one's risk level is a fact about the real artifact, not a guess.
LOW = {
    "age": 28,
    "systolic_bp": 118,
    "diastolic_bp": 76,
    "body_temp_f": 98.2,
    "heart_rate": 82,
    "hemoglobin": 12.1,
    "blood_glucose": 92,
    "stress_score": 3,
    "phys_activity_score": 6,
}
MEDIUM = {
    "age": 30,
    "systolic_bp": 138,
    "diastolic_bp": 88,
    "body_temp_f": 98.6,
    "heart_rate": 95,
    "hemoglobin": 10.5,
    "blood_glucose": 110,
    "stress_score": 5,
    "phys_activity_score": 4,
}
HIGH = {
    "age": 35,
    "systolic_bp": 185,
    "diastolic_bp": 125,
    "body_temp_f": 103.0,
    "heart_rate": 130,
    "hemoglobin": 6.0,
    "blood_glucose": 250,
    "stress_score": 9,
    "phys_activity_score": 1,
}


SOURCES = ["WHO recommendations on antenatal care"]
# What the fake web search returns by default: one official-looking page.
CITATIONS = [
    {"title": "National antenatal nutrition guideline", "url": "https://www.health.gov.example/antenatal.pdf"}
]
SOURCE_NAME = "National antenatal nutrition guideline (health.gov.example)"


def plan_json(*, meals=None, activities=None, extra_tip=None, sources=SOURCES, official=None) -> str:
    """Both sections together, as a test would like to write them. ``FakeModel``
    hands each section to the model call that asked for it."""
    nutrition = {
        "meals": meals
        or [
            {"slot": "breakfast", "item_key": "oats", "text": "Oats with milk"},
            {"slot": "lunch", "item_key": "lentils", "text": "Lentils with roti"},
        ],
        "foods_to_eat": [{"item_key": "spinach", "text": "Spinach"}],
        "foods_to_avoid": [{"item_key": "raw_egg", "text": "Raw eggs"}],
        "hydration": "Drink water through the day.",
        "timing_tips": ["Eat at regular times."] + ([extra_tip] if extra_tip else []),
    }
    exercise = {
        "activities": activities
        or [
            {
                "item_key": "walking",
                "text": "A gentle walk",
                "duration_minutes": 20,
                "frequency_per_week": 5,
                "intensity": "light",
            }
        ],
        "avoid": [{"item_key": "heavy_lifting", "text": "Heavy lifting"}],
        "stop_signs": ["Dizziness"],
    }
    if sources is not None:
        nutrition["sources"] = list(sources)
        exercise["sources"] = list(sources)
    # The pages the model says it used: by default the ones the fake search returns.
    pages = [c["url"] for c in CITATIONS] if official is None else list(official)
    nutrition["official_pages"] = pages
    exercise["official_pages"] = pages
    return json.dumps({"nutrition": nutrition, "exercise": exercise})


def advice_json(*, tips=None, contact=False, sources=SOURCES) -> str:
    data = {
        "tips": tips or ["Rest and drink water.", "Avoid heavy activity until your next check."],
        "contact_care_team": contact,
    }
    if sources is not None:
        data["sources"] = list(sources)
    return json.dumps(data)


def section_of(prompt: str) -> str | None:
    """Which request a care-plan prompt is (None: some other feature's prompt)."""
    if "You write SHORT advice" in prompt:
        return "advice"
    if "You write the NUTRITION plan" in prompt:
        return "nutrition"
    if "You write the PHYSICAL-ACTIVITY plan" in prompt:
        return "exercise"
    return None


def answer_for(prompt: str, reply: str | None) -> str | None:
    """A combined ``plan_json`` reply narrowed to the section this prompt asked for;
    anything that is not a combined plan (garbage, None) is passed through as is."""
    if reply is None:
        return None
    try:
        data = json.loads(reply)
    except ValueError:
        return reply
    section = section_of(prompt)
    if isinstance(data, dict) and section in data:
        return json.dumps(data[section])
    return reply


class FakeModel:
    """Stands in for ``openrouter_client.generate``.

    The AI Summary feature calls the same function on every reading, so this
    answers (and counts) only the care-plan prompts and returns None -- the
    "model unavailable" answer -- to everything else.

    A regeneration is TWO requests, one per section. ``count`` is the number of
    regenerations (nutrition requests); ``calls_for`` gives each section's prompts.
    ``reply`` is a string or a callable(prompt); ``reply_for`` overrides it per section.
    """

    def __init__(self):
        self.reply = plan_json()
        self.reply_for = {}
        self.calls = []
        # The pages the live web search "found" for a plan request (``[]``: found nothing).
        self.citations = [dict(c) for c in CITATIONS]
        self.web_flags = []  # web_search flag of every researched (plan) request
        self.search_down = False  # True: a request WITH web search fails, one without works
        self.prose_when_searching = False  # True: with the search on, the model answers in prose, not JSON

    def researched(self, prompt, *, model, max_tokens, timeout=45.0, web_search=True):
        """Stands in for ``openrouter_client.generate_researched``."""
        if section_of(prompt) is None:
            return None
        self.web_flags.append(web_search)
        if web_search and self.search_down:
            return None
        if web_search and self.prose_when_searching:
            return Researched("I could not find anything, sorry.", self.citations)
        text = self(prompt, model=model, max_tokens=max_tokens, timeout=timeout)
        if text is None:
            return None
        return Researched(text, self.citations if web_search else [])

    def __call__(self, prompt, *, model, max_tokens, timeout=10.0):
        section = section_of(prompt)
        if section is None:
            return None
        self.calls.append(prompt)
        if section == "advice":  # the short per-reading advice has its own answer
            reply = self.reply_for.get("advice", advice_json())
            return reply(prompt) if callable(reply) else reply
        reply = self.reply_for.get(section, self.reply)
        reply = reply(prompt) if callable(reply) else reply
        return answer_for(prompt, reply)

    def calls_for(self, section: str) -> list[str]:
        return [p for p in self.calls if section_of(p) == section]

    @property
    def count(self) -> int:
        """How many weekly plans were written (nutrition requests)."""
        return len(self.calls_for("nutrition"))

    @property
    def advice_count(self) -> int:
        return len(self.calls_for("advice"))


@pytest.fixture
def fake_model():
    fake = FakeModel()
    with (
        patch("momcare_platform.core.ai.openrouter_client.generate", new=fake),
        patch("momcare_platform.core.ai.openrouter_client.generate_researched", new=fake.researched),
    ):
        yield fake


@pytest.fixture
def make_patient(make_hospital):
    def _make(hospital, first_name="Ayesha", *, weeks=20, **patient_extra):
        # A fixed date of birth: the age band comes from it, and the vitals above
        # carry different `age` values that would otherwise flip the band.
        patient_extra.setdefault("date_of_birth", timezone.now().date() - timedelta(days=365 * 28))
        return onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": first_name, "last_name": "Bibi", **patient_extra},
            pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=weeks)},
        )

    return _make


@pytest.fixture
def add_reading():
    def _add(patient, vitals, *, minutes_ago=0):
        pregnancy = patient.current_pregnancy
        return VitalReading.objects.create(
            pregnancy=pregnancy,
            source=VitalReading.SOURCE_MANUAL,
            recorded_at=timezone.now() - timedelta(minutes=minutes_ago),
            **vitals,
        )

    return _add


@pytest.fixture
def patient_user(db):
    """A patient's own app account, linked to a Patient row on request."""

    def _make(patient, email="mother@careplan.test", password="MotherPass!2026"):
        user = User.objects.create_user(
            email=email,
            password=password,
            first_name="Mother",
            last_name="User",
            role=Role.objects.get(code=settings.ROLE_PATIENT),
        )
        user.organization = patient.organization
        user.is_email_verified = True
        user.save(update_fields=["organization", "is_email_verified", "updated_at"])
        patient.user = user
        patient.save(update_fields=["user", "updated_at"])
        return SimpleNamespace(user=user, email=email, password=password)

    return _make


def current_week(patient):
    from momcare_platform.modules.pregnancy.care_plans.models import CareWeek  # noqa: PLC0415

    return CareWeek.objects.filter(care_plan__pregnancy=patient.current_pregnancy).order_by("-week_number").first()


def all_advice(patient):
    from momcare_platform.modules.pregnancy.care_plans.models import ReadingAdvice  # noqa: PLC0415

    return ReadingAdvice.objects.filter(care_plan__pregnancy=patient.current_pregnancy).order_by("created_at")


def current_plan(patient):
    from momcare_platform.modules.pregnancy.care_plans.models import CarePlan  # noqa: PLC0415

    return CarePlan.objects.filter(pregnancy=patient.current_pregnancy).order_by("-month_number").first()
