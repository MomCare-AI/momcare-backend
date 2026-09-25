"""``allocate_percentages``/``round_metric_value`` -- pure functions, no
Django involved, ported from Neuro_RPM's own ``guideline_bands.py``.
"""

from momcare_model.statistics import allocate_percentages, round_metric_value


def test_evenly_divisible_counts_split_cleanly():
    result = allocate_percentages({"a": 1, "b": 1}, 2)

    assert result == {"a": 50, "b": 50}


def test_thirds_do_not_lose_a_point_to_naive_rounding():
    """1/3, 1/3, 1/3 naively rounds to 33+33+33=99 -- the leftover point must
    go to one of the equal remainders, not vanish."""
    result = allocate_percentages({"a": 1, "b": 1, "c": 1}, 3)

    assert sum(result.values()) == 100
    assert result == {"a": 34, "b": 33, "c": 33}


def test_two_to_one_split_matches_the_reference_case():
    """Matches Neuro_RPM's own reference example: 2 of 3 in one band, 1 in
    another -> 67%/33%, not 66.67%/33.33% truncated to 66/33 (=99)."""
    result = allocate_percentages({"Normal": 2, "Tachycardia": 1}, 3)

    assert sum(result.values()) == 100
    assert result == {"Normal": 67, "Tachycardia": 33}


def test_zero_total_returns_all_zero_not_a_division_error():
    result = allocate_percentages({"a": 0, "b": 0}, 0)

    assert result == {"a": 0, "b": 0}


def test_every_key_is_present_even_at_zero_count():
    result = allocate_percentages({"a": 3, "b": 0, "c": 0}, 3)

    assert result == {"a": 100, "b": 0, "c": 0}


def test_percentages_always_sum_to_exactly_one_hundred():
    """Property check across a spread of uneven counts -- the whole point of
    largest-remainder allocation."""
    cases = [
        {"a": 7, "b": 3},
        {"a": 5, "b": 5, "c": 5, "d": 1},
        {"a": 1, "b": 1, "c": 1, "d": 1, "e": 1, "f": 1, "g": 1},
    ]
    for counts in cases:
        total = sum(counts.values())
        result = allocate_percentages(counts, total)
        assert sum(result.values()) == 100


# ── round_metric_value ────────────────────────────────────────────────────


def test_zero_decimal_metrics_round_to_the_nearest_whole_number_and_return_int():
    """125.9 rounds to 126, not 130 -- there is no round-to-nearest-10
    bucketing anywhere in this rule, only nearest-whole-number."""
    result = round_metric_value("systolic_bp", 125.9)

    assert result == 126
    assert isinstance(result, int)


def test_zero_decimal_metrics_round_to_the_nearer_whole_number():
    assert round_metric_value("heart_rate", 91.4) == 91
    assert round_metric_value("heart_rate", 91.6) == 92


def test_one_decimal_metrics_keep_one_decimal_and_return_float():
    result = round_metric_value("body_temp_f", 101.567)

    assert result == 101.6
    assert isinstance(result, float)


def test_every_zero_decimal_metric_returns_int():
    for metric in ["systolic_bp", "diastolic_bp", "heart_rate"]:
        assert isinstance(round_metric_value(metric, 88.5), int)


def test_every_one_decimal_metric_returns_float():
    for metric in ["body_temp_f", "blood_glucose", "hemoglobin", "stress_score", "phys_activity_score"]:
        assert isinstance(round_metric_value(metric, 88.5), float)


def test_an_unrecognized_metric_defaults_to_one_decimal():
    result = round_metric_value("some_future_vital", 12.345)

    assert result == 12.3
    assert isinstance(result, float)
