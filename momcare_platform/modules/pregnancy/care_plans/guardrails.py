"""Everything between the model's answer and a patient's screen.

The plan text is written by an LLM, so none of it can be trusted to obey a
prompt. These checks run in code, on every version, whatever the model said:

- the answer must parse and match the fixed schema;
- no medicine, supplement, dose or calorie/weight-target wording may appear
  (medication is a clinician's decision, never the AI's);
- foods matching a patient's allergens are removed -- the model is told about
  allergies too, but a prompt is a request, not a guarantee;
- activity is capped by risk level in code, so a high-risk patient can never be
  shown more than the gentle baseline, however the model answered.

Pure functions. NOT reviewed by a clinician: the banned-word list, the activity
allow-list and the caps below are product decisions awaiting sign-off.
"""

from __future__ import annotations

import copy
import json
import re

DISCLAIMER = (
    "This is general guidance generated for you, not a replacement for your provider's advice. "
    "Follow what your care team tells you."
)
WARNING_SIGNS = [
    "Severe headache",
    "Vaginal bleeding",
    "Reduced or no baby movement",
    "Blurred vision",
    "Chest pain or difficulty breathing",
]

# ── banned content ────────────────────────────────────────────────────────

_BANNED = [
    r"\bmedication(s)?\b",
    r"\bmedicine(s)?\b",
    r"\bdrug(s)?\b",
    r"\bprescri\w+",
    r"\btablet(s)?\b",
    r"\bcapsule(s)?\b",
    r"\bpill(s)?\b",
    r"\bsyrup(s)?\b",
    r"\binjection(s)?\b",
    r"\bsupplement\w*",
    r"\bmultivitamin(s)?\b",
    r"\bprenatal vitamin(s)?\b",
    r"\bfolic acid\b",
    r"\bdos(e|es|age)\b",
    r"\b\d+(\.\d+)?\s?(mg|mcg|µg|ug|iu)\b",
    r"\bcalorie(s)?\b",
    r"\bkcal\b",
    r"\bweight[- ](gain|loss|target)\b",
    r"\b(lose|gain) weight\b",
    r"\bbmi\b",
]
_BANNED_RE = re.compile("|".join(_BANNED), re.IGNORECASE)


def find_banned(text: str) -> list[str]:
    """Every banned phrase found in ``text`` (empty list = clean)."""
    return [m.group(0) for m in _BANNED_RE.finditer(text or "")]


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def banned_in_content(content) -> list[str]:
    found: list[str] = []
    for text in _strings(content):
        found.extend(find_banned(text))
    return found


# ── item keys ─────────────────────────────────────────────────────────────

# One standard key per food/exercise, so "oats" and "oatmeal" count as the same
# thing when corrections are tallied. The model is told to emit standard keys;
# this folds the common variants it still produces.
_SYNONYMS = {
    "oatmeal": "oats",
    "porridge": "oats",
    "porridge_oats": "oats",
    "rolled_oats": "oats",
    "chapati": "roti",
    "chapatti": "roti",
    "phulka": "roti",
    "curd": "yogurt",
    "dahi": "yogurt",
    "yoghurt": "yogurt",
    "lentil": "lentils",
    "dal": "lentils",
    "daal": "lentils",
    "egg": "eggs",
    "walk": "walking",
    "brisk_walking": "walking",
    "light_walking": "walking",
    "stretch": "stretching",
    "prenatal_yoga": "gentle_yoga",
    "yoga": "gentle_yoga",
    "kegels": "pelvic_floor_exercises",
    "kegel_exercises": "pelvic_floor_exercises",
    "breathing": "breathing_exercises",
}


def normalize_item_key(raw) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", str(raw or "").strip().lower()).strip("_")
    return _SYNONYMS.get(key, key)


# ── schema ────────────────────────────────────────────────────────────────

_NUTRITION_LISTS = ("meals", "foods_to_eat", "foods_to_avoid")
_INTENSITIES = ("light", "moderate", "vigorous")

# A plan is advice a nurse could hand over, not an essay. The prompt asks for
# at most 300 words of nutrition and 120 of exercise (about 450 in all); these
# are the hard limits, with headroom for a model that runs a little over. An
# answer beyond them is rejected (one retry, then the fallback) rather than
# shown, and over-long lists are trimmed to the sizes below.
NUTRITION_MAX_WORDS = 380
EXERCISE_MAX_WORDS = 160
LIST_LIMITS = {
    "meals": 6,
    "foods_to_eat": 6,
    "foods_to_avoid": 6,
    "timing_tips": 3,
    "activities": 4,
    "avoid": 4,
    "stop_signs": 5,
}
_NOT_PROSE = {
    "item_key",
    "slot",
    "intensity",
    "day",
    "duration_minutes",
    "frequency_per_week",
    "sources",
    "source_links",
    "basis",
}


def visible_word_count(node) -> int:
    """Words a patient would actually read in ``node`` -- keys, slots and numbers
    are not prose and are not counted."""
    if isinstance(node, str):
        return len(node.split())
    if isinstance(node, dict):
        return sum(visible_word_count(v) for k, v in node.items() if k not in _NOT_PROSE)
    if isinstance(node, list):
        return sum(visible_word_count(v) for v in node)
    return 0


class InvalidPlan(ValueError):
    """The model's answer cannot be shown. ``str(exc)`` says why (logged, never shown)."""


def parse_model_output(raw: str | None) -> dict:
    """Pull the JSON object out of a model's reply (tolerating a ```json fence)."""
    if not raw or not raw.strip():
        raise InvalidPlan("empty response")
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidPlan(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidPlan("top level is not an object")
    return data


def _check_items(items, label, *, text_key="text"):
    if not isinstance(items, list):
        raise InvalidPlan(f"{label} must be a list")
    cleaned = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get(text_key), str) or not item[text_key].strip():
            raise InvalidPlan(f"{label} has an item without text")
        item = dict(item)
        item["item_key"] = normalize_item_key(item.get("item_key") or item[text_key])
        if not item["item_key"]:
            raise InvalidPlan(f"{label} has an item without a usable key")
        cleaned.append(item)
    return cleaned


def validate_nutrition(nutrition) -> dict:
    """Return a normalised copy of a nutrition section, or raise ``InvalidPlan``."""
    if not isinstance(nutrition, dict):
        raise InvalidPlan("the nutrition section must be an object")
    out_n = {name: _check_items(nutrition.get(name, []), f"nutrition.{name}") for name in _NUTRITION_LISTS}
    out_n["hydration"] = str(nutrition.get("hydration") or "")
    tips = nutrition.get("timing_tips", [])
    if not isinstance(tips, list) or not all(isinstance(t, str) for t in tips):
        raise InvalidPlan("nutrition.timing_tips must be a list of strings")
    out_n["timing_tips"] = tips
    # No weekly rotation: it was the main thing that made plans long, and a short
    # plan a patient actually reads beats a thorough one she does not. The key
    # stays (empty) so the response shape is stable.
    out_n["weekly_rotation"] = []
    if not out_n["meals"]:
        raise InvalidPlan("nutrition.meals is empty")
    for name in ("meals", "foods_to_eat", "foods_to_avoid", "timing_tips"):
        out_n[name] = out_n[name][: LIST_LIMITS[name]]
    for name in ("foods_to_eat", "foods_to_avoid"):
        out_n[name] = [_name_the_food(item) for item in out_n[name]]
    return out_n


def _name_the_food(item: dict) -> dict:
    """A list entry must say WHICH food: a model sometimes writes only the reason
    ("Rich in iron.") and keeps the food in its key. Put the food in front."""
    key_words = [w for w in item["item_key"].split("_") if len(w) > 2]
    text = item["text"].lower()
    # A short text is a food name in the model's own words ("chapati" under the key
    # "roti"); only a longer phrase that never mentions the food is a bare reason.
    if not key_words or len(text.split()) < 4 or any(w[:5] in text for w in key_words):
        return item
    return {**item, "text": f"{item['item_key'].replace('_', ' ').capitalize()}: {item['text']}"}


def validate_exercise(exercise) -> dict:
    """Return a normalised copy of an exercise section, or raise ``InvalidPlan``."""
    if not isinstance(exercise, dict):
        raise InvalidPlan("the exercise section must be an object")
    activities = _check_items(exercise.get("activities", []), "exercise.activities")
    for activity in activities:
        intensity = str(activity.get("intensity", "light")).lower()
        activity["intensity"] = intensity if intensity in _INTENSITIES else "light"
        for numeric in ("duration_minutes", "frequency_per_week"):
            value = activity.get(numeric)
            if value is not None:
                try:
                    activity[numeric] = int(value)
                except (TypeError, ValueError) as exc:
                    raise InvalidPlan(f"exercise.activities {numeric} must be a whole number") from exc
    stop = exercise.get("stop_signs", [])
    if not isinstance(stop, list) or not all(isinstance(t, str) for t in stop):
        raise InvalidPlan("exercise.stop_signs must be a list of strings")
    return {
        "activities": activities[: LIST_LIMITS["activities"]],
        "avoid": _check_items(exercise.get("avoid", []), "exercise.avoid")[: LIST_LIMITS["avoid"]],
        "stop_signs": stop[: LIST_LIMITS["stop_signs"]],
    }


def validate_schema(data: dict) -> dict:
    """Both sections together (the shape stored plans and the fallback use)."""
    nutrition, exercise = data.get("nutrition"), data.get("exercise")
    if not isinstance(nutrition, dict) or not isinstance(exercise, dict):
        raise InvalidPlan("nutrition and exercise objects are required")
    return {"nutrition": validate_nutrition(nutrition), "exercise": validate_exercise(exercise)}


# -- sources: the authority each section says it is based on ------------------

#
# Where a plan came from is decided by CODE from what the live web search really
# returned -- never from names the model writes about itself (a model invents
# document titles and links). Three bases:
#   web_search  the model searched the web for the patient's country's guidance and
#               these are the pages it read (links the doctor can open);
#   ai_only     the search found nothing usable (or was unavailable): the plan is the
#               model's own work and is labelled as generated by AI;
#   baseline    MomCare's fixed generic plan (the model failed altogether).

MAX_SOURCES = 5
MAX_SOURCE_CHARS = 140
BASIS_WEB = "web_search"
BASIS_AI = "ai_only"
BASIS_BASELINE = "baseline"
FALLBACK_SOURCE = "MomCare generic safe baseline plan (written by the MomCare team, not personalised)"
AI_ONLY_SOURCE = (
    "No official guideline was found for this plan. It was generated by AI and has not been "
    "checked against a published guideline."
)
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_MD_LINK_RE = re.compile(r"\(?\s*\[[^\]]*\]\([^)]*\)\s*\)?")
_BARE_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def _host(url: str) -> str:
    match = re.match(r"https?://([^/?#]+)", url, re.IGNORECASE)
    host = (match.group(1) if match else "").lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _source_title(title: str, host: str) -> str:
    """A readable name for a page: its title if that is short and clean, else its site.
    Page titles from the web are often junk ("© Designed By ...", a list of authors)."""
    title = " ".join((title or "").split())
    if not title or "©" in title or len(title) > 110 or find_banned(title) or re.match(r"^[A-Z]\)\s", title):
        return host
    return title


_GOVERNMENT_LABELS = {"gov", "gob", "gouv", "govt", "go", "nhs"}
_ACADEMIC_LABELS = {"edu", "ac"}


def is_official_host(host: str) -> bool:
    """Is this site run by a government, an international body, a university or a
    non-profit/professional society? A heuristic on the site's own name, so a clinic
    blog, a shop or an app ("holisticare.pk", "x.lovable.app", "bumpbites.health")
    never counts as an official source however well a search ranks it. The clinician
    remains the check; this only keeps commercial pages out of the "official" list."""
    host = (host or "").lower()
    if host.endswith((".gov", ".int", ".edu", ".org", ".mil", ".nhs.uk")) or host == "nhs.uk":
        return True
    labels = host.split(".")
    inner = labels[1:-1]  # not the first label (a bare "go.com") and not the top-level domain
    return any(label in _GOVERNMENT_LABELS | _ACADEMIC_LABELS for label in inner)


def _normal_url(url: str) -> str:
    return url.strip().split("#")[0].rstrip("/").lower()


def build_sources(citations, official_pages=None) -> dict:
    """``{"basis", "sources", "source_links"}`` from the pages a web search really
    returned (``[{"title", "url"}]``).

    A page counts as a source only if the model says it used it for the plan
    (``official_pages``: it picks from the pages it was given -- a URL that was never
    returned by the search is ignored) AND its site passes ``is_official_host``. No page
    left means the plan is the model's own work, and says so."""
    chosen = {_normal_url(u) for u in official_pages or [] if isinstance(u, str)}
    links: list[dict] = []
    seen: set[str] = set()
    for cite in citations or []:
        url = str(cite.get("url") or "").strip() if isinstance(cite, dict) else ""
        if not re.match(r"https?://[^\s/]+", url, re.IGNORECASE) or url in seen:
            continue
        host = _host(url)
        if _normal_url(url) not in chosen or not is_official_host(host):
            continue
        seen.add(url)
        links.append({"title": _source_title(str(cite.get("title") or ""), host), "url": url, "host": host})
    links = links[:MAX_SOURCES]
    if not links:
        return {"basis": BASIS_AI, "sources": [AI_ONLY_SOURCE], "source_links": []}
    names = [
        (f"{link['title']} ({link['host']})" if link["title"] != link["host"] else link["host"])[:MAX_SOURCE_CHARS]
        for link in links
    ]
    return {"basis": BASIS_WEB, "sources": names, "source_links": links}


def baseline_sources() -> dict:
    return {"basis": BASIS_BASELINE, "sources": [FALLBACK_SOURCE], "source_links": []}


def strip_links(node):
    """Remove web links and "[title](url)" citations the model wrote into its own text
    (a web-searching model likes to). The real sources are recorded separately."""
    if isinstance(node, str):
        text = _BARE_URL_RE.sub("", _MD_LINK_RE.sub("", node))
        text = re.sub(r"\s+([.,;:])", r"\1", text)
        return " ".join(text.split())
    if isinstance(node, dict):
        return {k: strip_links(v) for k, v in node.items()}
    if isinstance(node, list):
        return [strip_links(v) for v in node]
    return node


# ── allergies ─────────────────────────────────────────────────────────────


_MEAT_AND_FISH = (
    "chicken", "meat", "mutton", "beef", "lamb", "goat", "pork", "turkey", "duck", "liver", "bacon", "ham",
    "sausage", "fish", "seafood", "prawn", "shrimp", "tuna", "salmon", "rohu", "crab", "lobster", "gelatin",
)  # fmt: skip
_ANIMAL_PRODUCTS = (
    "milk", "yogurt", "yoghurt", "curd", "dahi", "lassi", "cheese", "paneer", "butter", "ghee", "cream",
    "egg", "honey", "whey",
)  # fmt: skip


def excluded_foods(allergens, dietary_preference: str = "") -> list[str]:
    """Everything this patient must never be told to eat: her allergens, plus meat and
    fish for a vegetarian and also dairy, eggs and honey for a vegan. Enforced in code
    on the model's answer exactly like an allergy -- the prompt asks for it too, but a
    live test showed a model telling a vegetarian to eat chicken."""
    foods = [a for a in (allergens or []) if a and str(a).strip()]
    if dietary_preference in ("vegetarian", "vegan"):
        foods += _MEAT_AND_FISH
    if dietary_preference == "vegan":
        foods += _ANIMAL_PRODUCTS
    return foods


def _allergen_pattern(allergens):
    tokens = [re.escape(a.strip().lower()) for a in allergens if a and a.strip()]
    if not tokens:
        return None
    # Optional plural "s"/"es" so "peanut" matches "peanuts" and "egg" matches "eggs".
    return re.compile(r"\b(" + "|".join(tokens) + r")(e?s)?\b", re.IGNORECASE)


def remove_allergens_from_nutrition(nutrition: dict, allergens) -> tuple[dict, list[str]]:
    """Drop every food the patient is allergic to from what she is told to eat.

    ``foods_to_avoid`` is left alone on purpose: naming an allergen there is
    correct. Returns the cleaned copy and the removed texts (kept in the
    version's inputs for audit, never shown).
    """
    pattern = _allergen_pattern(allergens)
    if pattern is None:
        return nutrition, []
    nutrition = copy.deepcopy(nutrition)
    removed: list[str] = []
    for name in ("meals", "foods_to_eat"):
        kept = []
        for item in nutrition[name]:
            if pattern.search(item["text"]) or pattern.search(item["item_key"].replace("_", " ")):
                removed.append(item["text"])
            else:
                kept.append(item)
        nutrition[name] = kept
    for day in nutrition.get("weekly_rotation", []):
        kept_meals = []
        for meal in day["meals"]:
            if pattern.search(meal):
                removed.append(meal)
            else:
                kept_meals.append(meal)
        day["meals"] = kept_meals
    return nutrition, removed


def remove_allergens(content: dict, allergens) -> tuple[dict, list[str]]:
    """Both-sections wrapper of ``remove_allergens_from_nutrition``."""
    nutrition, removed = remove_allergens_from_nutrition(content["nutrition"], allergens)
    return {**content, "nutrition": nutrition}, removed


# ── activity caps ─────────────────────────────────────────────────────────

# High risk: only this gentle set, light intensity, short bouts, until a
# clinician widens it. Enforced here, not left to the model.
GENTLE_BASELINE_ACTIVITIES = frozenset(
    {
        "walking",
        "stretching",
        "breathing_exercises",
        "relaxation",
        "pelvic_floor_exercises",
        "prenatal_stretching",
    }
)
_CAPS = {
    # risk -> (max intensity, max minutes)
    "high": ("light", 15),
    "medium": ("light", 30),
    "low": ("moderate", 45),
}
_INTENSITY_RANK = {"light": 0, "moderate": 1, "vigorous": 2}


def cap_exercise(exercise: dict, risk: str) -> dict:
    """Enforce the per-risk activity ceiling on a copy of an exercise section."""
    exercise = copy.deepcopy(exercise)
    max_intensity, max_minutes = _CAPS.get(risk, _CAPS["high"])  # unknown risk -> strictest
    activities = exercise["activities"]
    if risk not in _CAPS or risk == "high":
        activities = [a for a in activities if a["item_key"] in GENTLE_BASELINE_ACTIVITIES]
        if not activities:
            activities = [
                {
                    "item_key": "walking",
                    "text": "Short, gentle walks at an easy pace, resting whenever you need to.",
                    "duration_minutes": 10,
                    "frequency_per_week": 5,
                    "intensity": "light",
                }
            ]
    for activity in activities:
        if _INTENSITY_RANK.get(activity.get("intensity", "light"), 0) > _INTENSITY_RANK[max_intensity]:
            activity["intensity"] = max_intensity
        minutes = activity.get("duration_minutes")
        if minutes is not None and minutes > max_minutes:
            activity["duration_minutes"] = max_minutes
    exercise["activities"] = activities
    return exercise


def cap_activity(content: dict, risk: str) -> dict:
    """Both-sections wrapper of ``cap_exercise``."""
    return {**content, "exercise": cap_exercise(content["exercise"], risk)}


# ── fallback ──────────────────────────────────────────────────────────────


def fallback_content(risk: str) -> dict:
    """The safe generic plan shown when the model fails or stays invalid. Written
    by hand, so it can be read in full -- and it goes through the same caps."""
    # Plant-based on purpose: the fallback knows nothing about this patient, and a
    # vegetarian or vegan patient must never be handed meat, fish, egg or dairy. Her
    # allergies are removed afterwards (services.safe_fallback); foods to AVOID may
    # still name meat, eggs or dairy -- telling someone not to eat them is always safe.
    nutrition = {
        "meals": [
            {
                "slot": "breakfast",
                "item_key": "whole_grains",
                "text": "Whole-grain roti or bread with a piece of fruit",
            },
            {"slot": "lunch", "item_key": "lentils", "text": "Lentils or beans with vegetables and rice or roti"},
            {"slot": "snack", "item_key": "fruit", "text": "A piece of fresh fruit or a handful of seeds"},
            {"slot": "dinner", "item_key": "vegetables", "text": "Cooked vegetables with lentils or beans and roti"},
        ],
        "foods_to_eat": [
            {"item_key": "leafy_greens", "text": "Leafy green vegetables such as spinach"},
            {"item_key": "fruit", "text": "A variety of fresh fruit"},
            {"item_key": "pulses", "text": "Lentils, beans and chickpeas"},
        ],
        "foods_to_avoid": [
            {"item_key": "raw_or_undercooked_food", "text": "Raw or undercooked meat, fish and eggs"},
            {"item_key": "unpasteurised_dairy", "text": "Unpasteurised milk and cheese"},
            {"item_key": "excess_salt", "text": "Very salty or heavily processed foods"},
        ],
        "hydration": "Drink water regularly through the day.",
        "timing_tips": ["Eat small meals at regular times rather than long gaps between meals."],
        "weekly_rotation": [],
        **baseline_sources(),
    }
    exercise = {
        "activities": [
            {
                "item_key": "walking",
                "text": "A gentle walk at an easy pace",
                "duration_minutes": 20,
                "frequency_per_week": 5,
                "intensity": "light",
            }
        ],
        "avoid": [
            {"item_key": "heavy_lifting", "text": "Heavy lifting"},
            {"item_key": "contact_sports", "text": "Contact sports and activities with a risk of falling"},
        ],
        "stop_signs": ["Dizziness", "Chest pain", "Bleeding", "Shortness of breath", "Regular contractions"],
        **baseline_sources(),
    }
    return cap_activity({"nutrition": nutrition, "exercise": exercise}, risk)


_NEUTRAL_HYDRATION = "Drink water through the day."


def scrub_banned(content: dict) -> dict:
    """Drop the single entries that carry banned wording instead of throwing the whole
    plan away. A model that has just read an official guideline likes to repeat its
    "folic acid" and "calories" lines; losing one tip is better than a patient falling
    back to the generic plan. Nothing banned survives (``banned_in_content`` still runs
    after this); a plan left with no meals or no activities is invalid."""
    content = copy.deepcopy(content)
    for name, entries in list(content.items()):
        if isinstance(entries, list) and name != "weekly_rotation":
            content[name] = [e for e in entries if not banned_in_content(e)]
        elif name == "hydration" and find_banned(str(entries)):
            content[name] = _NEUTRAL_HYDRATION
    if "meals" in content and not content["meals"]:
        raise InvalidPlan("every meal carried banned wording")
    if "activities" in content and not content["activities"]:
        raise InvalidPlan("every activity carried banned wording")
    return content


def finalize_section(raw: str | None, section: str, *, risk: str, allergens, citations=None) -> tuple[dict, list[str]]:
    """One section's raw model text -> (safe content with its sources, removed-for-allergy
    texts), or ``InvalidPlan``. Nutrition and exercise are separate answers from
    separate model calls, each validated, limited and (on failure) replaced by its
    own fallback. ``citations`` are the pages the web search really returned; with none,
    the content says it was generated by AI."""
    data = parse_model_output(raw)
    if isinstance(data.get(section), dict):  # tolerate the answer being wrapped in its own name
        data = data[section]
    official_pages = data.get("official_pages")  # read before links are stripped from the text
    data = strip_links(data)
    sources = build_sources(citations, official_pages)
    if section == "nutrition":
        content = validate_nutrition(data)
        limit = NUTRITION_MAX_WORDS
    elif section == "exercise":
        content = validate_exercise(data)
        limit = EXERCISE_MAX_WORDS
    else:
        raise InvalidPlan(f"unknown section {section!r}")

    content = scrub_banned(content)
    banned = banned_in_content(content)
    if banned:
        raise InvalidPlan(f"banned content: {sorted(set(b.lower() for b in banned))}")
    words = visible_word_count(content)
    if words > limit:
        raise InvalidPlan(f"too long: {words} words in {section} (limit {limit})")

    removed: list[str] = []
    if section == "nutrition":
        content, removed = remove_allergens_from_nutrition(content, allergens)
        if not content["meals"]:
            raise InvalidPlan("every meal conflicted with the patient's allergies")
    else:
        content = cap_exercise(content, risk)
    content.update(sources)
    return content, removed


# ── reading advice ────────────────────────────────────────────────────────
# The short, immediate recommendations shown when a reading is medium/high or worse
# than the week's plan. Same rules as a plan -- sources, banned wording, allergies --
# plus: at high risk "contact your care team" is set by code, never left to the
# model, and any tip suggesting strenuous activity is dropped.

ADVICE_MAX_TIPS = 4
ADVICE_MAX_TIP_WORDS = 30
ADVICE_MAX_WORDS = 110
CONTACT_MESSAGE = "Please contact your care team today."
_STRENUOUS_RE = re.compile(
    r"\b(jog\w*|run|running|swim\w*|cycl\w*|lift\w*|vigorous\w*|intens\w*|workout\w*|gym|aerobic\w*|sprint\w*|hik\w*)\b",
    re.IGNORECASE,
)
_AVOIDING_RE = re.compile(r"\b(avoid|do not|don't|limit|skip|stay away|not eat)\b", re.IGNORECASE)


def finalize_advice(raw: str | None, *, risk: str, allergens, sources: dict | None = None) -> dict:
    """The model's short advice -> safe content, or ``InvalidPlan``. ``sources`` is
    the weekly plan's own (basis, sources, source_links): the advice is drawn from the
    plan she is following, which was researched, so it carries the same sources."""
    data = strip_links(parse_model_output(raw))
    tips = data.get("tips")
    if not isinstance(tips, list) or not all(isinstance(t, str) for t in tips):
        raise InvalidPlan("tips must be a list of strings")

    cleaned = [" ".join(t.split()) for t in tips if t.strip()]
    banned = [b for t in cleaned for b in find_banned(t)]
    if banned:
        raise InvalidPlan(f"banned content in advice: {sorted(set(b.lower() for b in banned))}")

    pattern = _allergen_pattern(allergens)
    if pattern is not None:
        # Naming an allergen to AVOID is fine; telling her to eat it is not.
        cleaned = [t for t in cleaned if not (pattern.search(t) and not _AVOIDING_RE.search(t))]
    if risk == "high":
        cleaned = [t for t in cleaned if not _STRENUOUS_RE.search(t)]
    cleaned = cleaned[:ADVICE_MAX_TIPS]
    if not cleaned:
        raise InvalidPlan("no usable tip left")
    if (
        any(len(t.split()) > ADVICE_MAX_TIP_WORDS for t in cleaned)
        or sum(len(t.split()) for t in cleaned) > ADVICE_MAX_WORDS
    ):
        raise InvalidPlan("advice too long")

    return {
        "tips": cleaned,
        "contact_care_team": bool(data.get("contact_care_team")) or risk == "high",
        **(sources or build_sources([])),
    }


def fallback_advice(risk: str) -> dict:
    """The safe generic advice when the model fails: short, food- and drug-free."""
    if risk == "high":
        tips = [
            "Rest now and avoid any strenuous activity.",
            "Drink water and keep your next meal light and low in salt.",
            "Do not wait if you feel worse: seek care straight away.",
        ]
    else:
        tips = [
            "Rest and drink water.",
            "Avoid heavy activity until your next check.",
            "Tell your care team if you start to feel worse.",
        ]
    return {"tips": tips, "contact_care_team": risk == "high", **baseline_sources()}
