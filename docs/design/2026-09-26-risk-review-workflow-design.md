# Risk review workflow — pending / reviewed / escalated

26 Sep 2026.

## What this is

Adapted from Neuro_RPM's Reading Review workflow (`patients/services.py`
`resolve_reading()`/`review_reading()`/`escalate_reading()`/`bulk_resolve_readings()`,
`PatientReading.ReviewStatus`, and the Patient List's `?workflow=reading_review` +
`dashboard-kpis`). The trigger is different — Neuro_RPM's is a configurable per-org/
location/patient `DataBound` threshold on a raw vital value; MomCare's is the trained
model's own confidence score (`flagged_for_review`, unchanged from its original
13 Sep 2026 introduction) — but the human workflow on top of that trigger (a pending
queue, two named triage actions, bulk resolution) is the same shape in both systems, and
this feature ports that shape without reintroducing a thresholds rules engine, which
MomCare deliberately does not have (see `MEMORY.md`'s "no rules engine" decision).

## What changed on `RiskAssessment`

`review_status` values renamed: `unreviewed`/`confirmed`/`corrected` →
`pending`/`reviewed`/`escalated`, matching Neuro_RPM's own naming exactly
(`migrations/0016_rename_review_status_values.py`, data migration:
`unreviewed→pending`, `confirmed→reviewed`, `corrected→reviewed` — a corrected
assessment was already fully handled by the doctor who corrected it under the old
single-action `verify` endpoint, which had no way to produce an "escalated" row, so no
existing row silently becomes one).

This is a deliberate narrowing of what the field name encodes. The old names tracked
*agreement* (did the doctor's answer match the model's); the new names track *triage
urgency* (has anyone looked, and if so, does this need to go further). Those are
genuinely different facts — a doctor can agree with the model and still want to escalate
to a specialist, or correct the model's number and consider the case closed. The old
fact isn't deleted: `confirmed_risk_level` is untouched, and "did they agree" is still
answerable by comparing it to `final_risk_level`. It's just no longer what
`review_status` itself says, and which of the two terminal values an assessment lands on
is now chosen by which action the clinician calls, not derived from that comparison.

`needs_review` (pre-existing property, originally `is_actionable and review_status ==
PENDING`) was re-pointed at the renamed constant here, then changed again the same day —
see the "`needs_attention`" follow-up section below for why its shape itself changed too.

## Endpoints

Replaces the single `POST .../risk/{id}/verify/` with two actions
(`modules/pregnancy/vitals/api/views.py`):

- `POST /pregnancies/{id}/risk/{assessment_id}/review/`
- `POST /pregnancies/{id}/risk/{assessment_id}/escalate/`

Both `IsClinician`-gated, both require `confirmed_risk_level` in the body (one of
low/medium/high) — this requirement is MomCare's own and predates this feature (the old
`VerifyRiskView`'s docstring: "there is no 'just seen, not confirmed' state"); it is
deliberately **not** relaxed to match Neuro_RPM's own review/escalate, which take no
risk-level input at all. Both guard on `RiskAssessment.needs_attention` (see the
follow-up section below) plus still-pending: an assessment that needs neither attention
condition, or one already resolved, is a 400 with a message naming which failed.

### Escalate is a label only

This was the one open design question, resolved explicitly with the user rather than
assumed: does `escalate` do anything beyond setting the status? Investigation into
Neuro_RPM's actual code found the honest answer for their own system — no. Their
`escalate_reading()` calls the identical `resolve_reading()` as `review_reading()`; the
only difference is which string is written. Their own code comment admits the richer
"Escalation workflow" this was meant to feed was never built.

MomCare already has a real, independent escalation system (`Alert`/`AlertEvent`, tiers,
the `escalate_alerts` cron) that Neuro_RPM doesn't. The option of wiring `escalate` here
into that ladder (bump the pregnancy's live Alert a tier, log an `AlertEvent` with the
clinician as actor) was raised and explicitly declined: `escalate` on a `RiskAssessment`
stays a pure triage label, matching Neuro_RPM's own behavior, on the reasoning that a
flagged assessment doesn't always have a live Alert to act on in the first place (a
flagged Low-risk reading raises no `Alert` at all — `is_actionable` gates that
independently), so the two systems would need to special-case a "nothing to bump" path
regardless, and conflating a clinician's manual triage note with the automated ladder's
own tier state was judged more confusing than useful.

## Follow-up same day: `needs_attention` — actionable OR flagged, one workflow not two

First shipped with the queue and the review/escalate guard both keyed on
`flagged_for_review` alone. Walking the user through the actual behavior surfaced a real
gap: a flagged **Low**-risk assessment (the model was unsure even about a Low verdict)
could appear in the queue but then 400 on `review`/`escalate`, since those required
`is_actionable` (non-Low) — a dead end the queue shouldn't have surfaced. Separately, the
user asked for Medium/High cases to appear in the queue **regardless of confidence**,
which the original `flagged_for_review`-only filter didn't do either.

Resolved by asking directly: does this need to be one workflow or two separate ones,
given severity (Medium/High) and model confidence are independent signals that Neuro_RPM
itself never has to reconcile (their own trigger, `is_out_of_range`, is a single raw-value
threshold with no confidence score behind it — they have no trained model gating this
workflow, so this dual-condition question simply doesn't arise for them; there was no
precedent to copy). **One workflow.** Both conditions resolve through the identical
`review`/`escalate` action — there's no second action, no different resolution mechanic
for "flagged" versus "actionable" — so two separate queues/endpoints would only duplicate
that resolution logic for no behavioral difference.

`RiskAssessment.needs_attention` is the single OR condition both places now read off:

```python
@property
def needs_attention(self) -> bool:
    return self.is_actionable or self.flagged_for_review
```

`needs_review` (pre-existing) becomes `needs_attention and review_status == PENDING` —
same shape, re-pointed at the new property rather than redefined from scratch. Both the
Queue's query and `resolve_risk_review()`'s guard now key off this identical condition, so
they can't drift apart the way they briefly did.

## Risk Review Queue

`GET /risk-review-queue/` — patients whose current pregnancy has at least one pending
assessment matching `needs_attention`: **actionable (Medium/High) OR flagged for low
model confidence**, whatever the level. Patient-centric (one row per patient, with that
patient's most recent qualifying assessment embedded), matching Neuro_RPM's own
`?workflow=reading_review` roster filter on their Patient list — but shaped as its own
standing endpoint rather than a query param, matching this project's own convention for
a queue (`AlertListView`, `PatientWorklistView`) rather than Neuro_RPM's viewset-filter
convention. Scoped identically to `AlertListView`
(`pregnancy__patient__location__organization` via `patient__location__organization` on
Pregnancy directly, plus `?assigned_to=me` via the shared `scope_to_assigned_staff()`).

Each result carries a `reasons` array (`["actionable"]`, `["low_confidence"]`, or both) so
a clinician can tell at a glance which condition put a patient there, since one list now
answers two different questions.

**Named and renamed the same day.** Shipped first as `AttentionQueueView`/
`/attention-queue/`, reusing a name a previous session had already coined in a
`PatientWorklistView` docstring for this exact unbuilt concept. On review, that name sat
oddly next to the rest of this feature — `review_status`, `review`/`escalate` — and,
more importantly, risked being read as a rename of the separate, already-existing `Alert`
model/endpoint, which this has nothing to do with. Renamed to `RiskReviewQueueView`/
`/risk-review-queue/` to read as one coherent feature instead of two disconnected names.

No separate counts-only endpoint (Neuro_RPM's `dashboard-kpis`). The pagination
envelope's `count` already is the KPI number — the same convention `AlertListView` uses
for its own `unacknowledged` badge. Neuro_RPM needed a second endpoint only because their
list and their count use different scoping (the list defaults to the live/pending subset,
the KPI is independent of any list filter); here they're the same query, so a second
endpoint would just duplicate it.

"Out of range" was considered and rejected as an alternate name for `flagged_for_review`
(raised by the user, who then asked for a recommendation): that phrase already means
something else in Neuro_RPM (a *value* outside a bound) and would misdescribe MomCare's
trigger, which fires on uncertainty, not on an extreme reading.

## Bulk review

`POST /risk/bulk-review/` — `{"items": [{"assessment_id", "review_status":
"reviewed"|"escalated", "confirmed_risk_level"}, ...]}`. Ported from Neuro_RPM's
`bulk_resolve_readings()`: each item resolves to its own target status in one atomic
transaction; a repeated id with a different status is a conflict, raised before anything
is touched; any single failure (not found, not accessible, not actionable, already
resolved) rolls back every write the call made. `queryset` is the caller's own
hospital-scoped `RiskAssessment` queryset — an id outside it reads as not-found, not
403, same as everywhere else in this project.

## Testing

24 new/updated tests in `modules/pregnancy/vitals/tests/api/test_risk.py`: review,
escalate-is-label-only (asserts the pregnancy's live Alert tier is untouched and no
`AlertEvent` was written), the already-resolved guard, the neither-condition guard
(renamed from the original Low-risk-only guard), a flagged-Low-can-be-reviewed case
proving the fix, six Risk Review Queue tests (flagged-and-actionable with both reasons,
excludes resolved, includes unflagged-actionable, includes flagged-Low, excludes
neither-condition, cross-tenant isolation), and three bulk-review tests (per-item status,
all-or-nothing rollback, cross-tenant isolation). 776 backend tests total (whole suite),
up from 767.

## Out of scope

- No Alert side effect from `escalate` (see above).
- No `DataBound`-style configurable thresholds — the model's confidence is the only
  uncertainty trigger, matching MomCare's existing "no rules engine" decision.
- No separate `dashboard-kpis` endpoint — the Risk Review Queue's own pagination count
  serves that purpose.
- No second, separate "confidence review" workflow — one `needs_attention` condition,
  one queue, one set of actions (see the follow-up section above).
