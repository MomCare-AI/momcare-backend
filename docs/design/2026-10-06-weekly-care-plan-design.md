# Weekly nutrition and exercise plan + per-reading advice — Design

**Date:** 2026-10-06
**Status:** Decided with the product owner; supersedes the "regenerate the whole plan on every reading" part of
`2026-10-05-care-plan-design.md`. Everything else in that document (monthly Care Plan as the container for
medication and notes, staff edits, allergies, statuses, hospital preferences, sources, guardrails) still stands.

## Why

Regenerating the full plan whenever a reading changed made plans noisy for the patient and unreviewable for the
doctor. A weekly plan is stable, practical (she can shop and cook for it) and matches how a clinician reviews a
trend. But a week is too slow to react to a bad reading, so a second, light layer reacts per reading.

## The two layers

| Layer | When | What it gives |
|---|---|---|
| **Weekly plan** | At the start of each **pregnancy week**, written at 12 am local time (where she lives) by the scheduled sweep, or by the first reading of the week if that arrives first | A progress summary, then the full nutrition and exercise plan for the week |
| **Reading advice** | On a reading that is **medium or high risk** (or worse than the week's plan) and whose condition differs from the last advice | 2–4 short tips and, when needed, "contact your care team". It never replaces the weekly plan. |

## Weeks

- A week is a **pregnancy week**: week *w* runs from day `w × 7` to `w × 7 + 6` counted from day 1
  (`EDD − 280`), i.e. gestational "17w0d – 17w6d" is week 17. Trimester boundaries (14w0d, 28w0d) are week starts.
- The monthly 30-day Care Plan stays the container for medication and notes. A weekly plan belongs to the
  monthly plan that contains the **week's first day**, so a week that straddles a month edge is not split.
- "Current plan" for a pregnancy = the plan holding the week that contains today (else the latest plan).

## Weekly plan: how it is built

1. **Gather**: last week's readings, this care-plan month's readings, and the latest reading as the current state.
   A week with no readings uses **the whole month's readings**; a month with none uses her most recent readings
   (the plan says so). A pregnancy with no readings at all gets no plan ("absent is not zero").
2. **Progress summary, computed by code** (never by the model, which could invent a percentage):
   - last week: number of readings; % low / medium / high risk; the most frequent abnormal vital categories
     ("Blood pressure — Stage 2 in 4 of 6 readings"); average vitals;
   - this month: recent readings versus earlier readings in the month → **trend** (improved / steady / worse, from
     the mean risk severity, threshold 0.15) and the change in the share of low-risk readings in percentage points;
   - areas to work on this week (the abnormal vital categories in her current state);
   - a plain-language text assembled from those facts ("Last week you had 6 readings: 33% low, 50% medium, 17%
     high … Compared with earlier this month your readings are improving: low-risk readings went from 20% to 33%
     (+13 points) … Areas to work on this week: blood pressure, hemoglobin.").
   Stored as JSON (`CareWeek.progress`: `facts` and `text`) and returned with the plan.
3. **Generate nutrition and exercise** as two separate model requests, as before (sources, word limits, allergy
   removal, activity caps by risk, banned-content checks, per-section fallback). The progress facts and the areas
   to work on are given to the model so the plan responds to them. Nutrition = the day's meals and foods for the
   week; exercise = what to do and how often this week.
4. **Store** as the week's versions; a new weekly plan returns a reviewed/finalized monthly plan to `in_progress`.

## During the week: reading advice and persistence

For each new reading, compared with the **baseline state the week's plan was written for**:

| Change | What happens |
|---|---|
| Structural (allergies, dietary preference, pregnancy factors, age band) | The week's plan is regenerated **immediately** (a newly recorded allergy cannot wait) |
| **Worse** (higher risk or a vital category worse) or risk medium/high | **Reading advice** is generated when the condition differs from the last advice's. The weekly plan is **not** changed. |
| Same or better and low risk | Nothing |

**Persistence → re-plan (counted in readings only; there is no time rule).** If the
worse-than-plan condition is still there on the very next reading — **3 consecutive worse
readings** — the week's plan is regenerated for the current state (so the first worse reading gets a
quick tip and a third one in a row changes the plan). A reading that is no longer worse resets the
count. The re-plan keeps the week's progress summary and records why it happened. The number is the
constant `PERSISTENCE_MIN_READINGS` in `services.py`.

The old "tighten at once / loosen after two readings" rule is gone: tightening is now the reading advice
(immediate, short) plus persistence; loosening happens at the next weekly plan.

## Reading advice

- Looks at the **weekly plan she is following** (its foods to avoid and activities) and her current condition.
- Short: 2–4 tips, at most about 100 words, in plain language; sources and a timestamp like every other output.
- **High risk always carries "contact your care team today"** (set by code, not left to the model); medium
  carries it when the model says so. At high risk, any tip suggesting strenuous activity is dropped in code, and
  tips naming an allergen are removed. No medicines, supplements, doses or calorie targets (same banned check).
- Fallback when the model fails: a short generic tip set by risk, naming MomCare's baseline as source.
- Stored per reading (`ReadingAdvice`); the plan response returns the latest advice for the current week.

## Data model changes

| Change | Detail |
|---|---|
| New `CareWeek` | `care_plan`, `week_number`, `week_start`, `week_end`, `progress` JSON, `baseline_state` JSON + key, `worse_since`, `worse_readings`, `last_advice_state_key`, `replans`, `last_replan_reason`. Unique (`care_plan`, `week_number`). |
| New `ReadingAdvice` | `care_plan`, `week`, `reading`, `risk_level`, `state_key`, `content` JSON (`tips`, `contact_care_team`, `sources`), `model_name`, `is_fallback`, `created_at`. |
| `CarePlanSectionVersion` | gains `week` (nullable FK to `CareWeek`) |
| `CarePlan` | the loosen-streak fields are removed; `current_state` mirrors the current week's baseline so workflows can read the risk level |
| RLS | both new tables get the same tenant policy (via the care plan) |

## API additions

The plan response (`GET /care-plans/{id}/`, `GET /pregnancies/{id}/current-care-plan/`) gains:
`week` (number, start, end, why it was last re-planned), `progress` (`text` + `facts`), `reading_advice` (latest for
the current week, or `null`) and `weeks` (the plan's weeks with their trend). No new endpoints.

## Workflows

- `care_plan_review`: the current plan is `in_progress` **and** its risk level is medium or high (low-risk weekly
  plans do not need a doctor). Reviewed once per week, not per reading.
- `care_plan_missing`: active pregnancy with no weekly plan covering today (no readings yet).

## Not changed / out of scope

Monthly container, medication (providers only), notes, allergies write-through, adjustments surviving
regeneration, corrections → hospital preferences, sources and `sources_verified: false`, the clinical-review caveat
(thresholds, persistence numbers, severity trend threshold and banned words are product decisions not yet reviewed
by a clinician).


## The week starts at 12 am, and failures are retried

- "Today" is the date **in the hospital location's time zone**, so a pregnancy week begins at 12 am where she
  lives, not at the server's midnight.
- The scheduled sweep (`care_plans.sweep` Celery task, or `manage.py sweep_care_plans` under cron — every 15
  minutes, so every time zone's midnight is caught within minutes) writes the new week's plan for every
  pregnancy that has readings and no plan for the week she is now in. It does not wait for a reading.
- **If it fails, it is simply created again.** A week whose creation failed leaves nothing behind (it is
  rolled back) and is created again on the next run. A week that was written from the generic fallback because
  the AI service failed is retried every run until the real, personalised plan is stored (only the stuck section
  is redone). One patient's failure never stops anyone else's week from starting.


## Official guidance: a live web search for every plan (2026-10-06, later)

The plan is no longer written from the model's memory of "what authorities say". **Every nutrition and exercise
request turns on OpenRouter's web search** (`openrouter_client.generate_researched`, the `web` plugin, 5 results).
The prompt orders the model to search first for the official guidance of **the patient's country** (health ministry or
national health authority, or its national obstetric/nutrition society), prefer that country's own pages, use WHO only
to fill gaps, ignore other countries' pages, and write cautious general advice if nothing official is found. No
guideline text is stored in the code and there is no reference pack: each request researches for itself.

**Where a plan came from is recorded by code, never written by the model.** The pages OpenRouter returns as
`url_citation` annotations become `source_links` (`title`, `url`, `host`; up to 5, de-duplicated; a junk page title is
replaced by the site name). The model's own "sources" are no longer asked for or kept (a model invents titles and
links), and links or `[title](url)` citations it writes into the plan text are stripped. Each section gets a `basis`:

| `basis` | Meaning | Shown to the patient/staff |
|---|---|---|
| `web_search` | the search returned pages the plan was written from | the page links |
| `ai_only` | no usable page was found, or the search itself failed and the request was repeated without it | `generated_by_ai_notice`: "No official guideline for your country was found for this plan. It was generated by AI." |
| `baseline` | the model failed altogether: MomCare's fixed generic plan | the baseline source line |

Quick reading advice does not search again (it would add seconds to saving a reading): it is drawn from the weekly plan
she is following and carries **that plan's** `basis`, `sources` and `source_links`.

`sources_verified` stays `false`: the pages are real, the content is not checked against them, and the clinician
remains the check. Cost measured on the real model: about 0.7 cent and 6 seconds per search request, so roughly 1.5
cents per patient per week; reading saves that trigger a plan take longer.

Also changed in the same pass: a list entry ("foods to eat/avoid") must name the food (a bare reason gets the food
put in front); the plan carries `contact_care_team` / `contact_message` (set by code) for as long as the latest
reading is high risk, even when the week's plan was written at high risk and so earns no new quick tip; and the
progress text no longer says "keep doing what you are doing" after a week that was not healthy, says "first plan" for a
brand-new patient, and compares a week at a month boundary with the week before it.


## Speed: parallel sections and a background worker (2026-10-06, later)

With the live web search a plan takes 16-18 s (two searches, one after the other, plus retries). Two changes:

1. **Nutrition and exercise are requested at the same time** (`run_section_requests`, a two-thread pool). Everything
   the requests need from the database is gathered first (`prepare_section_request`), so the threads touch no
   database -- the request itself is a plain HTTP call. Waiting is roughly halved.
2. **The plan is written by a Celery worker, not inside the reading's request** (`care_plans.process_assessment`,
   queued with `transaction.on_commit`, so a worker never sees an uncommitted reading). The reading saves in about
   2 s. `CARE_PLAN_GENERATE_IN_BACKGROUND` (default on; off in tests and local dev) writes it inline instead. If
   the queue is unreachable the plan is written inline rather than lost; a failing task is logged and the 15-minute
   sweep writes a week's first plan on its next run.

The API tells the screen what is happening, derived from the data (no new column): `preparing` (readings exist but
no plan covers this week yet), `update_pending` (her newest assessment is newer than the plan's last evaluation, for
up to 10 minutes) and a `status_message` ("Your plan is being prepared..." / "...being updated for your latest
reading"). A patient with no readings is never told a plan is being prepared.


## Progress per section (2026-10-06, later)

The weekly progress is no longer only one overall paragraph. Each section reports on **its own vitals**, computed by
code from the same readings (`progress.section_progress`), stored beside the overall summary in `CareWeek.progress`
(`progress["nutrition"]`, `progress["exercise"]`, each `{facts, text}`) and returned inside that section
automatically -- in the plan response and in the nutrition-only / exercise-only endpoints (`section.progress`).

| Section | Vitals it reports on |
|---|---|
| Nutrition | hemoglobin, blood glucose, blood pressure |
| Exercise | heart rate, physical-activity score, stress, blood pressure |

It states how many of last week's readings had one of its vitals outside the normal range and which, the change in
that share against the week before (or earlier in the month; a move of 10 percentage points counts), the areas to work
on, and never "keep doing what you are doing" after a bad week. Each section's model request is given its own text
(an older week stored before this falls back to the overall summary). Which vital belongs to which section is a
product decision awaiting clinical review.
