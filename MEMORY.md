# MEMORY.md — Decisions of Record

Every settled decision for MomCare's risk-prediction work, in one place.

**This file outranks assumptions.** If code, a docstring, or `CLAUDE.md` disagrees
with something here, this file is right and the other thing is stale.

Last updated: 2026-09-07 — model training and backend wiring are **done and
live**, on branch `feature/model-training`. This update supersedes every
"not built yet" item the previous version of this file listed.

---

## 1. What the system does

Predict a pregnant patient's risk — **Low / Medium / High** — from vitals
collected by a wearable band or entered by staff, in real time, for hospitals
using the MomCare platform. Every step below is implemented and tested against
a real Postgres database, not a prototype.

---

## 2. Status in one paragraph

The model is trained (`momcare_model/models/artifacts/v1/`), wired into the
live scoring path (`core.monitoring.services.reassess_risk()`), and reachable
through the real API (`POST /pregnancies/{id}/readings/`). Test accuracy is
**88.40%**, matching the locked training recipe. A known limitation exists —
see §9 — and is mitigated, not fixed, by the confidence threshold. The
obstetrician clinical review is the one gap nobody on this team can close by
writing more code; see §10.

---

## 3. The dataset — condensed

Three regional sources, merged into **10,512 rows** (8,408 train / 1,052
validation / 1,052 test). Africa's data has **zero Medium-risk examples**
(binary Low/High only) — this is *why* the Africa+Medium→High rule in §6
exists. America's provenance claim (NHANES) is weaker than its documentation
states and was never resolved — say so honestly if asked. Medium-risk is 5.1%
of the merged data, which is why the model is noticeably weaker at that class
(see the confusion matrix in `momcare_model/models/artifacts/v1/metadata.json`).

---

## 4. The model

| | |
|---|---|
| Algorithm | XGBoost, 3-class, trained on `SimpleImputer` + `SMOTE` (train split only) |
| Test accuracy | **88.40%** — reproduced exactly against the locked recipe |
| Region awareness | Region-blind. The model never sees region. |
| Class encoding | `Low = 0, Medium = 1, High = 2` — canonical everywhere, including sort keys. Never redefine; negate in the sort key if worst-first ordering is needed. |
| Artifact | `momcare_model/models/artifacts/v1/` — `xgboost_model.json`, `imputer.joblib`, `metadata.json`. `momcare_model/config.py::ACTIVE_MODEL_VERSION` selects which version loads. |

**The 9 features, in this exact order** (`momcare_model/config.py::FEATURE_COLS`):

`age`, `systolic_bp`, `diastolic_bp`, `body_temp_f`, `heart_rate`,
`hemoglobin`, `blood_glucose`, `stress_score`, `phys_activity_score`

Temperature is **Fahrenheit** everywhere. Never Celsius.

`momcare_model.predict(vitals: dict) -> dict | None` is the single inference
entry point. Returns `None` — not a fabricated guess — when every one of the
9 vitals is missing.

---

## 5. The five clinical categories — built, live, on every assessment

`bp_category` · `heart_rate_category` · `temperature_category` ·
`glucose_category` · `hemoglobin_category` — from `momcare_model/clinical_categories.py`,
the single shared source for both training-time and inference-time labels.

**Display-only. Never fed to the model.** NaN-safe: a missing vital returns
`""` (empty string), never a guessed default — the frontend must render that
as "not recorded," not as "Normal."

---

## 6. Postprocessing — after the model, in Python, never in the database

### 6a. Africa + Medium → shown as High

An Africa-region patient whose model prediction is **Medium** is presented as
**High**. Why: Africa's training data has zero Medium examples, so a Medium
prediction there has no ground truth behind it — escalating is the safe
direction. `risk_level` keeps the model's real answer; `final_risk_level` is
what's shown and acted on. Region comes from `Organization.region`, derived
from `country`, never stored as its own field, never a model input.

### 6b. Confidence threshold — currently **80%**, not 70%

| | |
|---|---|
| Platform default | **`Decimal("0.800")`** — `settings.MOMCARE_DEFAULT_CONFIDENCE_THRESHOLD` |
| Per-hospital override | `Organization.confidence_threshold` (nullable — null means "follow the platform default," a live state) |
| Always read via | `Organization.effective_confidence_threshold` |
| Below threshold | `RiskAssessment.flagged_for_review = True`, and `alerts.services.notify_low_confidence()` emails the assigned clinician **only** — never the patient |

Raised from 70%→80% on 2026-09-07 after running `momcare_model.evaluate`
against the real test set: 15.3% of true-High cases score as Low (81 of 530).
70% caught 23% of those for review; 80% catches 48%, at the cost of flagging
17.5% of all predictions instead of 8.9%. Of everything flagged at 80%,
**~68% turns out to be a false alarm** (the model was actually right, just not
confident) — that's the real, measured cost of this safety margin, not a bug.

**The prediction is never hidden or withheld** — always shown, flagged or not.
There is no "old `doctor_notified` boolean" anymore; the real field is
`flagged_for_review`, and the actual notification is a real email, not just a
flag.

---

## 7. Database — the two tables that matter

### `VitalReading` — one row per check-in (wide format)

`id · pregnancy · age · systolic_bp · diastolic_bp · heart_rate · body_temp_f ·
hemoglobin · blood_glucose · stress_score · phys_activity_score · source ·
recorded_at · device · recorded_by · created_at`

`source` is **required on every write**, one of exactly `"device"` /
`"manual"` — never inferred from whether a device happens to be assigned
(a nurse can manually enter a reading while a band is worn). There is no
third "simulated" value anymore — the old fake-data simulator was removed
entirely; `seed_demo`'s own generated history is tagged `"manual"`.

### `RiskAssessment` — one row per level *transition*, not per reading

`id · pregnancy · reading · risk_level · final_risk_level · previous_risk_level
· confirmed_risk_level · review_status · bp_category · heart_rate_category ·
temperature_category · glucose_category · hemoglobin_category · confidence ·
flagged_for_review · assessed_at · verified_at · verified_by`

| Column | Answers |
|---|---|
| `risk_level` | What the model actually said — raw, never overwritten |
| `final_risk_level` | What's actually shown/acted on — **use this one for display** |
| `previous_risk_level` | What it was last time — one row shows the whole transition |
| `confidence` | The model's own certainty, 0–1. **Null on old, pre-model rows** — the frontend must handle that gracefully, not crash on it |
| `flagged_for_review` | True when confidence fell below threshold — show a "needs doctor review" badge |
| `review_status` | `unreviewed` (default) → `confirmed` / `corrected`, derived, never chosen directly |

`reading` is a direct FK — one query returns the judgement and the vitals
behind it together (see the API response shape in §8).

---

## 8. API reference — for frontend integration

Full request/response examples, already run against a real database, live in
**`docs/api/MomCare Platform.postman_collection.json`** — import that into
Postman first; this section is the summary.

**Auth:** `POST /api/auth/login/` `{email, password}` → `{access, user}`.
Refresh token is an HttpOnly cookie, not in the body. Every other endpoint
needs `Authorization: Bearer {access}`. `POST /api/auth/refresh/` reads the
cookie and issues a new access token; `POST /api/auth/logout/` blacklists it.

**Record a reading — `POST /api/pregnancies/{id}/readings/`**
Body: any subset of the 9 vitals (at least one required) **plus required
`source`** (`"device"` or `"manual"`), optional `recorded_at`. Every vital has
a physiologically-plausible min/max — see `VITAL_BOUNDS` in
`momcare_platform/core/monitoring/api/serializers.py` — violating one returns
a 400 with a specific message, not a silent save.
Response = the saved reading, **plus** `risk_changed` (bool) and `risk_level`.
**Important gotcha:** `risk_level` here is `null` whenever the risk level
*didn't change* from the previous reading — even though a real prediction was
computed internally. If the frontend needs the definitive current state
regardless of whether it just changed, call `GET /risk/` instead of relying on
this field.

**`GET /api/pregnancies/{id}/readings/`** — paginated list, newest first.
**`GET /api/pregnancies/{id}/readings/latest/`** — `{reading, total_count}`,
`reading: null` when there isn't one yet (never fake a normal-looking value).

**`GET /api/pregnancies/{id}/risk/`** — `{current, history}`. Each item is a
full `RiskAssessment` (see §7's field table) with the `reading` nested inline.
**`POST /api/pregnancies/{id}/risk/`** — re-run scoring on demand (no new
reading needed); 201 with the new assessment if the level changed, 200 with
`{"detail": "No change in risk level.", "current": ...}` if not.
**`POST /api/pregnancies/{id}/risk/{assessment_id}/verify/`** — clinicians only
(not hospital_admin). Body: `{"confirmed_risk_level": "low"|"medium"|"high"}`.

**`GET /api/organization/me/`** — includes `confidence_threshold` (this
hospital's own override, or `null`) and `effective_confidence_threshold` (what
actually gets used — always prefer this one for display).
**`PATCH /api/organization/me/confidence-threshold/`** — hospital_admin only.
`{"confidence_threshold": 0.80}` to set; `{"confidence_threshold": null}` to
clear the override and follow the platform default again.

**Devices:** `GET`/`POST /api/devices/`, `POST`/`DELETE
/api/pregnancies/{id}/device/` to assign/unassign — standard CRUD, no
surprises.

**Universal gotchas:** every ID is a UUID string, never an integer. A request
against another hospital's pregnancy/patient resolves to **404, never 403** —
don't build error handling that expects a permission-denied response for
cross-tenant access.

---

## 9. Known limitation — measured, not fixed

15.3% of true High-risk cases (81 of 530 in the test set) score as Low. These
cases share a specific pattern: **elevated glucose + high stress score, while
BP/heart-rate/temperature/hemoglobin all look normal** — the model
under-weights this combination. Confirmed empirically: a rule-based
"safety net" checking individual vitals against clinical thresholds does
**not** fix this (it either catches nothing, or floods ~85% of all readings
with false flags) — the pattern is genuinely multivariate, not a single hard
cutoff. The confidence threshold (§6b) mitigates this, catching 48% of these
specific cases; it does not fix it. **The real fix is retraining with more
data covering this presentation** — flagged for later, not blocking anything
today. Run `momcare_model/evaluate.py` again after any retrain to check this
number specifically, not just overall accuracy.

---

## 10. What's still genuinely open

- **Obstetrician clinical review** — nobody with medical training has
  confirmed the 5 category thresholds, the escalation timings in
  `core/alerts/escalation.py`, or the model's behavior are sound for real
  pregnant patients. Documented as a hard requirement before real clinical
  use; not something engineering work can close.
- **Retraining** — see §9. Blocked on better/more data, not on approval.
- **The migration must be applied to production** — `manage.py migrate` was
  run locally and in tests, but Railway's production database has not had it
  applied yet as of this writing. The exact bug this would otherwise cause
  (`column "source" does not exist`) was caught and reproduced locally.
- **RLS doesn't actually protect production** — policies exist and are
  tested, but the production DB role has `BYPASSRLS`. Pre-existing gap,
  unrelated to the ML work; needs a dedicated non-superuser app role.
- **`v2` has never existed** — the versioning system (`ACTIVE_MODEL_VERSION`,
  per-version artifact folders) is built but unexercised; only `v1` has ever
  been trained.

---

## 11. Undecided

Whether to store the **threshold value in effect** on each `RiskAssessment`
at the moment it was scored. Without it, a past `flagged_for_review` can't be
explained after a hospital later changes its threshold, and it can't be
backfilled retroactively. Still unresolved — same question as before, nobody
has picked an answer yet.
