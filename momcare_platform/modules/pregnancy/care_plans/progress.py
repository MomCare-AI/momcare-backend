"""The weekly progress summary: how the patient is doing compared with before.

**Every number here is computed by code from her stored readings.** A language
model asked for "your improvement percentage" would produce a confident
figure whether or not it was true, so the model never writes these -- it is only
*given* the facts (to write a plan that responds to them), and the text the
patient reads is assembled from the same facts, so the two cannot disagree.

Pure functions: no database, no Django. The caller turns stored readings and
their risk assessments into ``ReadingRow`` objects.

The trend rule (mean risk severity, threshold 0.15) and the 4-reading minimum
for a within-month comparison are product decisions awaiting clinical review.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime

from .state import VITAL_AXES, VITAL_LABELS, category_severity, risk_severity

TREND_THRESHOLD = 0.15
MIN_ROWS_TO_SPLIT_A_MONTH = 4
RISKS = ("low", "medium", "high")

BASED_ON_LAST_WEEK = "last_week"
BASED_ON_MONTH = "month"
BASED_ON_LAST_READING = "last_reading"

TREND_IMPROVED = "improved"
TREND_STEADY = "steady"
TREND_WORSE = "worse"


@dataclass(frozen=True)
class ReadingRow:
    recorded_at: datetime
    risk: str
    vitals: dict = field(default_factory=dict)  # axis -> clinical category ("" = not recorded)
    values: dict = field(default_factory=dict)  # raw numbers for averages


def risk_percentages(rows: list[ReadingRow]) -> dict[str, int]:
    """% of readings at each risk level, as whole numbers that always add up to
    100 (largest-remainder rounding, so 33 / 33 / 34 and never 33 / 33 / 33)."""
    if not rows:
        return {}
    counts = Counter(row.risk for row in rows)
    exact = {risk: counts.get(risk, 0) * 100 / len(rows) for risk in RISKS}
    floors = {risk: int(value) for risk, value in exact.items()}
    leftover = 100 - sum(floors.values())
    for risk in sorted(RISKS, key=lambda r: exact[r] - floors[r], reverse=True)[:leftover]:
        floors[risk] += 1
    return floors


def mean_severity(rows: list[ReadingRow]) -> float:
    return sum(risk_severity(row.risk) for row in rows) / len(rows)


def concerns(rows: list[ReadingRow], limit: int = 3) -> list[dict]:
    """The most frequent abnormal vital categories ("Blood pressure: Stage 2 in 4
    of 6 readings"), most frequent first."""
    counts: Counter = Counter()
    for row in rows:
        for axis in VITAL_AXES:
            category = row.vitals.get(axis, "")
            if category and category_severity(axis, category) > 0:
                counts[(axis, category)] += 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], -category_severity(kv[0][0], kv[0][1])))
    return [
        {"axis": axis, "category": category, "count": count, "of": len(rows)}
        for (axis, category), count in ranked[:limit]
    ]


def averages(rows: list[ReadingRow]) -> dict:
    """Average of the numbers actually recorded (a reading without one is skipped)."""
    out: dict = {}
    for name, digits in (
        ("systolic_bp", 0),
        ("diastolic_bp", 0),
        ("heart_rate", 0),
        ("hemoglobin", 1),
        ("blood_glucose", 0),
    ):
        numbers = [float(row.values[name]) for row in rows if row.values.get(name) is not None]
        if numbers:
            mean = sum(numbers) / len(numbers)
            out[name] = round(mean) if digits == 0 else round(mean, digits)
    return out


def _window_facts(rows: list[ReadingRow]) -> dict:
    return {
        "readings": len(rows),
        "risk_percent": risk_percentages(rows),
        "concerns": concerns(rows),
        "averages": averages(rows),
    }


def trend(recent: list[ReadingRow], earlier: list[ReadingRow]) -> dict:
    """improved / steady / worse, from the mean risk severity; plus the change in
    the share of low-risk readings in percentage points."""
    delta = mean_severity(recent) - mean_severity(earlier)
    direction = (
        TREND_IMPROVED if delta <= -TREND_THRESHOLD else TREND_WORSE if delta >= TREND_THRESHOLD else TREND_STEADY
    )
    recent_low = risk_percentages(recent)["low"]
    earlier_low = risk_percentages(earlier)["low"]
    return {
        "direction": direction,
        "recent_readings": len(recent),
        "earlier_readings": len(earlier),
        "recent_low_percent": recent_low,
        "earlier_low_percent": earlier_low,
        "low_risk_points_change": recent_low - earlier_low,
    }


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _signed(n: int) -> str:
    return f"+{n}" if n > 0 else f"−{abs(n)}" if n < 0 else "0"


def build_progress(
    *,
    week_number: int,
    last_week: list[ReadingRow],
    month_before_week: list[ReadingRow],
    focus_axes: list[str],
    last_reading_at: datetime | None = None,
    prior_week: list[ReadingRow] | None = None,
    week_start: date | None = None,
) -> dict:
    """``{"facts": ..., "text": ...}`` for the plan of pregnancy week ``week_number``.

    ``last_week``: readings in the previous pregnancy week. ``month_before_week``:
    every reading of this care-plan month before the week starts (it includes
    ``last_week``). ``focus_axes``: the abnormal vitals in her current state. ``prior_week``: the week
    before last week, whatever care-plan month it fell in -- so a week at the start of a
    new month still has something to be compared with. ``week_start``: first day of the
    week being planned (tells a first-ever plan from a long gap).
    """
    earlier_than_last_week = [r for r in month_before_week if r not in last_week]
    against = "month" if earlier_than_last_week else "week_before"
    if not earlier_than_last_week:
        earlier_than_last_week = list(prior_week or [])
    based_on = BASED_ON_LAST_WEEK if last_week else BASED_ON_MONTH if month_before_week else BASED_ON_LAST_READING

    if last_week and earlier_than_last_week:
        recent, earlier = last_week, earlier_than_last_week
    elif not last_week and len(month_before_week) >= MIN_ROWS_TO_SPLIT_A_MONTH:
        ordered = sorted(month_before_week, key=lambda r: r.recorded_at)
        half = len(ordered) // 2
        recent, earlier = ordered[half:], ordered[:half]
    else:
        recent = earlier = []

    facts: dict = {
        "week_number": week_number,
        "based_on": based_on,
        "last_week": _window_facts(last_week) if last_week else None,
        "month": _window_facts(month_before_week) if month_before_week else None,
        "trend": trend(recent, earlier) if recent and earlier else None,
        "focus": list(focus_axes),
        "last_reading_at": last_reading_at.isoformat() if last_reading_at else None,
        "first_plan": bool(
            based_on == BASED_ON_LAST_READING
            and last_reading_at is not None
            and week_start is not None
            and last_reading_at.date() >= week_start
        ),
    }
    if facts["trend"]:
        facts["trend"]["against"] = against if last_week else "month"
    window = last_week or month_before_week
    sections = {
        name: section_progress(
            name,
            based_on=based_on,
            window=window,
            recent=recent,
            earlier=earlier,
            focus_axes=focus_axes,
            first_plan=facts["first_plan"],
            against=facts["trend"]["against"] if facts["trend"] else "month",
            week_number=week_number,
        )
        for name in SECTION_AXES
    }
    return {"facts": facts, "text": _text(facts, last_reading_at), **sections}


def _risk_sentence(window: dict) -> str:
    pct = window["risk_percent"]
    return f"{pct['low']}% low risk, {pct['medium']}% medium and {pct['high']}% high"


def _concern_sentence(window: dict) -> str:
    return "; ".join(
        f"{VITAL_LABELS[c['axis']].lower()} was {c['category']} in {c['count']} of {_plural(c['of'], 'reading')}"
        for c in window["concerns"]
    )


def _not_settled(facts: dict) -> bool:
    """True when the window she is judged on was clearly not healthy (a quarter or more of
    her readings medium/high risk) or her trend is getting worse: "keep doing what you are
    doing" would then be the wrong thing to tell her."""
    window = facts.get("last_week") or facts.get("month")
    if window and 100 - window["risk_percent"]["low"] >= 25:
        return True
    return bool(facts.get("trend") and facts["trend"]["direction"] == TREND_WORSE)


def _text(facts: dict, last_reading_at: datetime | None) -> str:
    n = facts["week_number"]
    parts: list[str] = []

    if facts["based_on"] == BASED_ON_LAST_WEEK:
        window = facts["last_week"]
        parts.append(
            f"Last week (week {n - 1}) you had {_plural(window['readings'], 'reading')}: {_risk_sentence(window)}."
        )
        if window["concerns"]:
            parts.append("Your main concerns were: " + _concern_sentence(window) + ".")
        else:
            parts.append("None of your vitals were outside the normal range.")
    elif facts["based_on"] == BASED_ON_MONTH:
        window = facts["month"]
        parts.append(
            f"There were no readings last week (week {n - 1}), so this plan is based on your readings earlier this "
            f"month: {_plural(window['readings'], 'reading')}, {_risk_sentence(window)}."
        )
        if window["concerns"]:
            parts.append("Your main concerns were: " + _concern_sentence(window) + ".")
    else:
        when = f" on {last_reading_at:%d %b %Y}" if last_reading_at else ""
        if facts.get("first_plan"):
            parts.append(
                f"This is your first plan, based on your latest reading{when}. We will update it as more readings "
                "come in."
            )
        else:
            parts.append(
                f"There are no readings from the last few weeks, so this plan is based on your last reading{when}."
            )

    t = facts["trend"]
    since = "the week before" if t and t.get("against") == "week_before" else "earlier this month"
    if t:
        a, b, diff = t["earlier_low_percent"], t["recent_low_percent"], t["low_risk_points_change"]
        if t["direction"] == TREND_IMPROVED:
            parts.append(
                f"Compared with {since} your readings are improving: low-risk readings went from {a}% to "
                f"{b}% ({_signed(diff)} points)."
            )
        elif t["direction"] == TREND_WORSE:
            parts.append(
                f"Compared with {since} your readings have been getting worse: low-risk readings went from "
                f"{a}% to {b}% ({_signed(diff)} points)."
            )
        else:
            parts.append(
                f"Compared with {since} your readings are about the same: low-risk readings were {a}% and "
                f"are now {b}%."
            )
    elif facts["based_on"] == BASED_ON_LAST_WEEK:
        parts.append("This is your first week of readings, so there is nothing earlier to compare with yet.")

    if facts["focus"]:
        parts.append("Areas to work on this week: " + ", ".join(VITAL_LABELS[a].lower() for a in facts["focus"]) + ".")
    elif facts.get("first_plan"):
        pass  # nothing to "keep doing" yet: this is her first plan
    elif _not_settled(facts):
        parts.append("Your readings were not all in the healthy range, so follow this plan carefully this week.")
    else:
        parts.append("Keep doing what you are doing.")

    parts.append(f"Here is your nutrition and exercise plan for week {n}.")
    return " ".join(parts)


# -- progress for ONE section --------------------------------------------------
# Nutrition can change hemoglobin, blood glucose and blood pressure (salt); activity can change
# heart rate, her activity score, stress and blood pressure. So each section reports on its own
# vitals only. Which vital belongs to which section is a product decision awaiting clinical review.

SECTION_AXES = {
    "nutrition": ("hemoglobin", "glucose", "bp"),
    "exercise": ("heart_rate", "activity", "stress", "bp"),
}
_SECTION_WORDS = {"nutrition": "diet-related", "exercise": "activity-related"}
SECTION_TREND_POINTS = 10  # a change of this many percentage points is a real movement


def _flagged(rows: list[ReadingRow], axes) -> list[ReadingRow]:
    """Readings with at least one of ``axes`` outside the normal range."""
    return [r for r in rows if any(category_severity(a, r.vitals.get(a, "")) > 0 for a in axes)]


def _flagged_percent(rows: list[ReadingRow], axes) -> int:
    return round(100 * len(_flagged(rows, axes)) / len(rows)) if rows else 0


def _section_concerns(rows: list[ReadingRow], axes, limit: int = 3) -> list[dict]:
    counts: Counter = Counter()
    for row in rows:
        for axis in axes:
            category = row.vitals.get(axis, "")
            if category and category_severity(axis, category) > 0:
                counts[(axis, category)] += 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], -category_severity(kv[0][0], kv[0][1])))
    return [
        {"axis": axis, "category": category, "count": count, "of": len(rows)}
        for (axis, category), count in ranked[:limit]
    ]


def section_progress(
    section: str,
    *,
    based_on: str,
    window: list[ReadingRow],
    recent: list[ReadingRow],
    earlier: list[ReadingRow],
    focus_axes: list[str],
    first_plan: bool,
    against: str,
    week_number: int,
) -> dict:
    """``{"facts", "text"}`` about one section's own vitals, from the same readings the
    overall summary uses. Computed by code, like everything in this module."""
    axes = SECTION_AXES[section]
    word = _SECTION_WORDS[section]
    names = ", ".join(VITAL_LABELS[a].lower() for a in axes)
    focus = [a for a in focus_axes if a in axes]
    flagged = len(_flagged(window, axes))
    facts: dict = {
        "section": section,
        "axes": list(axes),
        "based_on": based_on,
        "readings": len(window),
        "flagged_readings": flagged,
        "flagged_percent": _flagged_percent(window, axes),
        "concerns": _section_concerns(window, axes),
        "trend": None,
        "focus": focus,
    }
    if recent and earlier:
        recent_pct, earlier_pct = _flagged_percent(recent, axes), _flagged_percent(earlier, axes)
        change = recent_pct - earlier_pct
        facts["trend"] = {
            "direction": TREND_WORSE
            if change >= SECTION_TREND_POINTS
            else TREND_IMPROVED
            if change <= -SECTION_TREND_POINTS
            else TREND_STEADY,
            "recent_percent": recent_pct,
            "earlier_percent": earlier_pct,
            "points_change": change,
            "against": against,
        }

    parts: list[str] = []
    if based_on == BASED_ON_LAST_READING:
        if focus:
            parts.append("Your latest reading showed: " + ", ".join(VITAL_LABELS[a].lower() for a in focus) + ".")
        else:
            parts.append(f"Your latest reading was in the normal range for your {word} measures ({names}).")
    else:
        lead = (
            f"Last week (week {week_number - 1})"
            if based_on == BASED_ON_LAST_WEEK
            else "There were no readings last week, so looking at earlier this month"
        )
        if flagged:
            parts.append(
                f"{lead}, {flagged} of {len(window)} readings had a {word} measure outside the normal range ({names})."
            )
            top = "; ".join(
                f"{VITAL_LABELS[c['axis']].lower()} was {c['category']} in {c['count']} of {c['of']}"
                for c in facts["concerns"]
            )
            parts.append(f"Most often: {top}.")
        else:
            parts.append(f"{lead}, none of your {word} measures ({names}) were outside the normal range.")

    t = facts["trend"]
    if t:
        since = "the week before" if t["against"] == "week_before" else "earlier this month"
        verb = {
            TREND_IMPROVED: "is improving",
            TREND_WORSE: "is getting worse",
            TREND_STEADY: "is about the same",
        }[t["direction"]]
        parts.append(
            f"Compared with {since}, the share of readings with a {word} measure outside the normal range {verb}: "
            f"{t['earlier_percent']}% before, {t['recent_percent']}% now."
        )

    if focus:
        if based_on != BASED_ON_LAST_READING:  # that case already named them above
            parts.append("To work on this week: " + ", ".join(VITAL_LABELS[a].lower() for a in focus) + ".")
    elif first_plan:
        pass
    elif facts["flagged_percent"] >= 25 or (t and t["direction"] == TREND_WORSE):
        parts.append("Take extra care with this part of your plan this week.")
    else:
        parts.append("Keep doing what you are doing.")
    return {"facts": facts, "text": " ".join(parts)}
