"""_build_citations() -- matches the AI's already-generated text against
values we already know are real (never the other way around: the AI never
gets to say what it's referencing). A value/name that doesn't appear in the
text produces no citation; nothing is ever invented."""

from decimal import Decimal

from momcare_platform.core.ai.services import _build_citations


def _snapshot(**overrides):
    base: dict = {
        "latest_readings": {},
        "_latest_reading_id": None,
        "provider_name": None,
        "_provider_id": None,
        "nurse_name": None,
        "_nurse_id": None,
        "care_manager_name": None,
        "_care_manager_id": None,
    }
    base.update(overrides)
    return base


def test_the_bp_combo_appearing_in_text_produces_one_reading_citation():
    snapshot = _snapshot(
        latest_readings={"systolic_bp": 185, "diastolic_bp": 115},
        _latest_reading_id="reading-1",
    )

    citations = _build_citations(snapshot, "Her latest reading was 185/115, which is elevated.")

    assert citations == [{"text": "185/115", "type": "reading", "id": "reading-1"}]


def test_a_reading_value_not_mentioned_in_the_text_produces_no_citation():
    snapshot = _snapshot(
        latest_readings={"systolic_bp": 185, "diastolic_bp": 115},
        _latest_reading_id="reading-1",
    )

    citations = _build_citations(snapshot, "Her vitals were within normal limits this week.")

    assert citations == []


def test_a_scalar_reading_value_appearing_in_text_produces_a_reading_citation():
    snapshot = _snapshot(
        latest_readings={"heart_rate": 88},
        _latest_reading_id="reading-2",
    )

    citations = _build_citations(snapshot, "Her heart rate was 88 bpm at her last visit.")

    assert citations == [{"text": "88", "type": "reading", "id": "reading-2"}]


def test_no_reading_citation_when_there_is_no_latest_reading_id():
    snapshot = _snapshot(latest_readings={"heart_rate": 88}, _latest_reading_id=None)

    citations = _build_citations(snapshot, "Her heart rate was 88 bpm.")

    assert citations == []


def test_a_mentioned_provider_name_produces_a_staff_citation():
    snapshot = _snapshot(provider_name="Dr. Ahmed", _provider_id="staff-1")

    citations = _build_citations(snapshot, "Dr. Ahmed is her assigned provider.")

    assert citations == [{"text": "Dr. Ahmed", "type": "staff", "id": "staff-1"}]


def test_a_provider_name_never_mentioned_produces_no_citation():
    snapshot = _snapshot(provider_name="Dr. Ahmed", _provider_id="staff-1")

    citations = _build_citations(snapshot, "No care team is currently assigned.")

    assert citations == []


def test_multiple_care_team_names_each_produce_their_own_citation():
    snapshot = _snapshot(
        provider_name="Dr. Ahmed",
        _provider_id="staff-1",
        nurse_name="Nurse Sana",
        _nurse_id="staff-2",
        care_manager_name="Manager Bilal",
        _care_manager_id="staff-3",
    )

    citations = _build_citations(
        snapshot,
        "Dr. Ahmed, Nurse Sana, and Manager Bilal make up the care team.",
    )

    assert {c["text"] for c in citations} == {"Dr. Ahmed", "Nurse Sana", "Manager Bilal"}
    assert all(c["type"] == "staff" for c in citations)


def test_no_citations_when_nothing_in_the_snapshot_matches_the_text():
    snapshot = _snapshot()

    citations = _build_citations(snapshot, "This patient has no data on file yet.")

    assert citations == []


def test_a_decimal_whole_number_reading_value_matches_the_ais_natural_writing():
    """Real bug caught in live end-to-end testing: systolic_bp/diastolic_bp/
    heart_rate/etc. are DecimalField(decimal_places=2) -- the real stored
    value is Decimal('142.00'), but a real model naturally writes '142' in
    prose, never '142.00'. Matching only the raw Decimal string meant this
    never actually cited a reading in practice."""
    snapshot = _snapshot(
        latest_readings={"heart_rate": Decimal("96.00")},
        _latest_reading_id="reading-3",
    )

    citations = _build_citations(snapshot, "Her heart rate was 96 bpm today.")

    assert citations == [{"text": "96", "type": "reading", "id": "reading-3"}]


def test_a_decimal_fractional_reading_value_matches_the_ais_natural_writing():
    snapshot = _snapshot(
        latest_readings={"body_temp_f": Decimal("99.10")},
        _latest_reading_id="reading-4",
    )

    citations = _build_citations(snapshot, "Her temperature was 99.1°F.")

    assert citations == [{"text": "99.1", "type": "reading", "id": "reading-4"}]


def test_a_decimal_bp_combo_matches_the_ais_natural_writing():
    snapshot = _snapshot(
        latest_readings={"systolic_bp": Decimal("142.00"), "diastolic_bp": Decimal("91.00")},
        _latest_reading_id="reading-5",
    )

    citations = _build_citations(snapshot, "Her BP was 142/91 at her last visit.")

    assert citations == [{"text": "142/91", "type": "reading", "id": "reading-5"}]
