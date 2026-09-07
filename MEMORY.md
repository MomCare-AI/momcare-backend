# MEMORY.md — Decisions of Record

Every settled decision for MomCare's risk-prediction work, in one place.

**This file outranks assumptions.** If code, a docstring, or `CLAUDE.md` disagrees
with something here, this file is right and the other thing is stale.

Last updated: 2026-09-06

---

## 1. What the system does

Predict a pregnant patient's risk — **Low / Medium / High** — from vitals
collected by a wearable band, in real time, for hospitals using the MomCare
platform.

---

## 2. The dataset

Three regional sources, merged into **10,512 rows**.

| Region | Source | Rows | Known weakness |
|---|---|---|---|
| Africa | Tanzania, real hospital data, longitudinal (up to 8 antenatal visits) | 7,982 | **Binary labels only — Low/High. No Medium ever recorded.** |
| Asia | UCI Bangladesh maternal health dataset + 11 added columns | 1,014 | The added columns (stress, activity, diet) look synthetic, not measured |
| America | Claimed CDC NHANES; actually reconstructed from a PDF | 1,516 | Severely imbalanced — only **14 real High-risk patients** |

- Medium-risk is **5.1%** of the merged data.
- ~78 Africa columns used the string `"not_checked"` instead of blanks, so true
  missingness was much higher than a naive `.isnull()` count showed.
- America's provenance claim (NHANES) is **weaker than its documentation states**
  and remains unresolved. Say so honestly in the report.

---

## 3. Preprocessing — DONE, locked at Phase 2.16

- 12-field **canonical schema** — all three regions mapped to the same names/units.
- Governance/ethics audit: licences checked, PII and leakage columns dropped.
- `blood_glucose` **kept** despite only 2/3 region coverage — strong predictor
  wherever present. Weight/height/BMI/diet/sleep **dropped** (weak or too sparse).
- **SMOTE applied to the training split only** — never to validation or test.
- **Stratified 80/10/10 split**, preserving both region and risk-class proportions.
- **Zero patient leakage across splits — verified, not assumed.**

---

## 4. The model

| | |
|---|---|
| Algorithm | **XGBoost**, 3-class |
| Baseline comparison | Random Forest **88.02%** vs XGBoost **88.50%** → XGBoost chosen |
| Accuracy after Phase A drops | **88.40%** |
| Region awareness | **Region-blind.** The model never sees region. |

**Class encoding — canonical, used everywhere including sort keys:**

```
Low = 0    Medium = 1    High = 2
```

If a queue needs worst-first, **negate in the sort key** — never redefine the
encoding.

**The 9 features:**

`age`, `systolic_bp`, `diastolic_bp`, `body_temp_f`, `heart_rate`,
`hemoglobin`, `blood_glucose`, `stress_score`, `phys_activity_score`

Temperature is **Fahrenheit** everywhere. Never Celsius.

**Dropped from training (Phase A):**
- All `*_category_encoded` columns
- All `region_Africa` / `region_Asia` / `region_America` one-hot columns

---

## 5. The five clinical categories

Built from real medical guidelines — ACC/AHA (blood pressure), AHA (heart rate),
CDC/WHO (temperature), ADA (glucose), WHO (haemoglobin/anaemia).

`bp_category` · `heart_rate_category` · `temperature_category` ·
`glucose_category` · `hemoglobin_category`

**These are display-only. They are NEVER fed to the model.** They exist so a
nurse sees *"BP: Stage 2, Heart Rate: Tachycardia"* alongside the risk level.

- **NaN-safe by design**: a missing vital returns *unknown*, never a guessed
  default.
- The functions must be **shared with the training pipeline, not duplicated**, so
  training-time and inference-time categorisation cannot drift apart.

> **Open item:** these five columns were removed from `RiskAssessment` on
> 2026-09-06 during the rules-engine deletion. They need restoring — as the
> display layer they always were, sourced from the shared functions.

---

## 6. Postprocessing — a separate stage, after the model

The model outputs a raw risk level. The backend then applies these rules, in
Python — **never in the database**.

### 6a. Africa + Medium → show as High

An Africa-region patient whose model prediction is **Medium** is presented as
**High risk**.

**Why:** Africa's training data contains **zero** Medium-risk examples, so a
Medium prediction for an African patient has no ground truth behind it. Escalating
is the safe direction — the same principle used everywhere in this project when
forced to choose.

**How it's recorded — this is exactly why the risk-level columns are separate:**

| Column | Value | Meaning |
|---|---|---|
| `risk_level` | `medium` | What the model actually said — never overwritten |
| `final_risk_level` | `high` | What is acted on, after this rule |
| `doctor_notified` | `true` | A clinician is told |
| `confirmed_risk_level` | *(set later)* | What the doctor decided it really was |

> An earlier discussion framed this as *"Result inconclusive — requires clinician
> review."* **Superseded.** The decision is: show it as High.

Region is kept as **plain-text metadata** (`Africa`/`Asia`/`America`) purely so
this rule can run. It is never a model input.

### 6b. Confidence threshold

| | |
|---|---|
| Platform default | **70%** — `settings.MOMCARE_DEFAULT_CONFIDENCE_THRESHOLD` |
| Per-hospital override | `Organization.confidence_threshold` (nullable) |
| Resolution order | hospital's own value → else platform default |
| Always read via | `Organization.effective_confidence_threshold` |

- **≥ threshold** → result goes through, `doctor_notified = false`
- **< threshold** → result is **still recorded**, `doctor_notified = true`

Null on a hospital means *"follow the platform default"* as a live state — so
raising the default later reaches every hospital that never chose its own.

**The prediction is never hidden or withheld.** Research
([arxiv 2508.07617](https://arxiv.org/html/2508.07617)) found that AI which
*withholds* low-confidence predictions makes clinicians **under-treat** (42%
false-negative rate vs 31% unaided). Show the result **and** flag it. Do not
"improve" this into abstention.

**70% is an operational choice — there is no standard.** WHO's AI guidance is
principles-only; the FDA regulates sensitivity/specificity, not per-prediction
confidence. Benchmarks that do matter: published preeclampsia models run 42–81%
sensitivity / 87–92% specificity; FDA-authorised AI devices median ~91%/~91%.

---

## 7. Database — the two tables

### `VitalReading` — one row per check-in (wide format)

`id` · `pregnancy` · `age` · `systolic_bp` · `diastolic_bp` · `heart_rate` ·
`body_temp_f` · `hemoglobin` · `blood_glucose` · `stress_score` ·
`phys_activity_score` · `recorded_at` · `source` · `device` · `recorded_by` ·
`created_at`

One row per check-in, **not** one row per vital — the band reports everything
together at one moment. Every vital is nullable: a row records what was actually
known, never a carried-over value presented as fresh.

`source` here = provenance of the **numbers** (device / manual / simulated).

### `RiskAssessment` — one row per level change

`id` · `pregnancy` · `reading` · `risk_level` · `final_risk_level` ·
`previous_risk_level` · `confirmed_risk_level` · `review_status` · `confidence` ·
`doctor_notified` · `verified_at` · `verified_by` · `assessed_at`

**The four risk-level columns, and why each exists:**

| Column | Answers |
|---|---|
| `risk_level` | What did the model say, raw and unedited? |
| `final_risk_level` | What is actually being acted on? (after Africa+Medium etc.) |
| `previous_risk_level` | What was it last time? — so one row shows the transition |
| `confirmed_risk_level` | What did the doctor say it really was? |

**`review_status`**: `unreviewed` (default, and the common permanent case) →
`confirmed` (doctor agreed) → `corrected` (doctor did not). Derived by comparing
`confirmed_risk_level` to `final_risk_level`; never chosen directly by the caller.
There is no "seen but not confirmed" state.

`reading` is a direct FK, so one query returns the judgement and the vitals
behind it.

---

## 8. Standing architecture rules

- **No rules engine. No if/then risk scoring.** The trained model is the only
  thing that may produce a risk level. `risk_rules.py` was deleted 2026-09-06.
- **`reassess_risk()` in `core/monitoring/services.py` is currently a no-op** and
  returns `None`. No assessments are created anywhere — including production —
  until the model lands. Every pregnancy reports "not assessed." **This is
  deliberate, not a bug.**
- **Business rules live in Python, not the database.** The DB stores values; it
  decides nothing.
- **Region is derived from country** (`core/common/regions.py`), never asked for
  on a form, never stored on a model. Unsupported country returns `None`, not a
  default.
- **Gestational age is derived from EDD on every read**, never stored.
- **Single repo** — ML training lives inside `Momcare_Backend`, not a separate
  project.
- Cross-tenant reads return **404, never 403**.

---

## 9. What does not exist yet

- **The ML project folder.** Training currently lives in a Colab notebook at
  `E:\FINAL YEAR PROJECT\DataPre Processing\COLLAB CODE\Untitled0 (1).ipynb`,
  outside this repo. Structure to follow cookiecutter-data-science.
- **A saved model artifact.** The `.fit()` has been run; nothing was exported.
  Needs: the model (native XGBoost format), the fitted imputer it depends on, and
  metadata (accuracy, training date, exact feature list, git commit).
- **`predict(patient_data: dict) -> dict`** — the single clean inference entry point.
- **The postprocessing layer as real code** — Africa+Medium and the confidence
  flag are decided in words, not yet written.
- **The five category columns on `RiskAssessment`** — see §5.
- **RLS actually protecting production** — policies exist, but the production DB
  role bypasses them. Pre-existing gap.

---

## 10. Undecided

- Whether to store the **threshold in effect** on each `RiskAssessment`. Without
  it, a past flag can't be explained after a hospital changes its threshold — and
  it cannot be backfilled later.
- Target accuracy, if any.
