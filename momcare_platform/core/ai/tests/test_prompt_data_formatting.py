"""_format_snapshot_value() -- how a field's raw Python value gets turned
into text inside the prompt. Real bug caught in live end-to-end testing:
latest_readings/thirty_day_average are dicts of Decimal objects, and
before this fix the prompt showed the model a raw Python repr like
"{'systolic_bp': Decimal('142.00'), ...}" -- nothing telling it blood
pressure is conventionally written as one "142/91" pair, so its own
phrasing varied unpredictably call to call, which is exactly why citation
matching (looking for that same "142/91" pair in the finished text) missed
it more often than not."""

from decimal import Decimal

from momcare_platform.core.ai.services import _format_snapshot_value


def test_a_decimal_scalar_formats_as_a_plain_number_not_a_decimal_repr():
    assert _format_snapshot_value(Decimal("99.10")) == "99.1"


def test_a_reading_dict_combines_systolic_and_diastolic_into_one_bp_pair():
    value = {
        "systolic_bp": Decimal("142.00"),
        "diastolic_bp": Decimal("91.00"),
        "heart_rate": Decimal("96.00"),
    }

    formatted = _format_snapshot_value(value)

    assert "blood_pressure: 142/91" in formatted
    assert "heart_rate: 96" in formatted
    assert "Decimal(" not in formatted


def test_a_reading_dict_with_no_bp_values_still_formats_cleanly():
    value = {"heart_rate": Decimal("88.00")}

    formatted = _format_snapshot_value(value)

    assert formatted == "heart_rate: 88"


def test_bp_keys_present_but_none_valued_does_not_crash_and_is_skipped():
    """Real bug caught in live end-to-end testing: systolic_bp/diastolic_bp
    are nullable fields -- both keys can be present in the dict with None
    values (not merely missing), which crashed _natural_number_str(None)
    before this fix."""
    value = {"systolic_bp": None, "diastolic_bp": None, "heart_rate": Decimal("88.00")}

    formatted = _format_snapshot_value(value)

    assert formatted == "heart_rate: 88"


def test_a_dict_where_every_value_is_none_shows_not_on_file():
    value = {"systolic_bp": None, "diastolic_bp": None}

    formatted = _format_snapshot_value(value)

    assert formatted == "not on file"


def test_an_empty_dict_still_shows_not_on_file():
    assert _format_snapshot_value({}) == "not on file"


def test_a_nested_dict_like_risk_this_month_flattens_without_raising():
    value = {
        "counts": {"low": 0, "medium": 0, "high": 1},
        "most_common": "high",
        "total_count": 1,
    }

    formatted = _format_snapshot_value(value)

    assert "most_common: high" in formatted
    assert "total_count: 1" in formatted
