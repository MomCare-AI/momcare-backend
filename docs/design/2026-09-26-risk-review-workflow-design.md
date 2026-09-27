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

## Second follow-up same day: split into two queues after all

The single combined queue above didn't survive contact with the user's actual mental
model, and the disagreement was worth having. The user pushed back on "one workflow" by
showing Neuro_RPM's live dashboard: two visually separate tiles, "Reading Review" and
"Out of Range." Investigating that screenshot's actual backing code confirmed neither
tile is a severity/confidence split — both read the identical `is_out_of_range` signal
(`DataBound`, a configurable raw-value threshold), differing only by time window
("Reading Review" is all-time pending; "Out of Range" is narrowed to the current month's
last 7 days). So the screenshot didn't settle the question either way — Neuro_RPM has no
precedent for combining or splitting severity and confidence, because it never has to
reconcile them.

Settled instead by separating two things this design had conflated: **the list** (how a
clinician browses and counts what needs attention) and **the resolution mechanism** (what
happens when they act on an entry). The "one workflow" argument was really an argument
against duplicating the *resolution* — and that argument still holds. But splitting the
*list* costs nothing extra: a clinician genuinely does think about "who's currently
Medium/High" and "where did the model hedge" as two different questions, each worth its
own place to look and its own count. So: two queues, one shared resolution.

`RiskAssessment.needs_attention` (`is_actionable or flagged_for_review`) still exists —
it's what `resolve_risk_review()`'s guard uses, since an assessment reachable from either
queue must be resolvable. Only the *queries* changed, from one OR-condition query to two
single-condition queries.

## Risk Review Queue & Low Confidence Queue

Two endpoints sharing one `_RiskQueueView` base (`modules/pregnancy/vitals/api/views.py`)
for scoping/pagination/`reasons`, differing only in the single `Q` condition each
subclass's `condition(prefix)` builds — `prefix` lets the identical expression work both
unprefixed (querying `RiskAssessment` directly, for the per-pregnancy subquery) and
prefixed with `risk_assessments__` (querying `Pregnancy`), so there's one definition per
queue instead of two that could drift apart from each other.

- `GET /risk-review-queue/` (`RiskReviewQueueView`) — `final_risk_level in [Medium, High]`.
  Severity only, regardless of confidence.
- `GET /low-confidence-queue/` (`LowConfidenceQueueView`) — `flagged_for_review`.
  Confidence only, regardless of level (a flagged Low shows up here).

Both patient-centric (one row per patient, with that patient's most recent qualifying
assessment embedded), matching Neuro_RPM's own `?workflow=reading_review` roster-filter
*pattern* — but shaped as standing endpoints rather than a query param, matching this
project's own convention for a queue (`AlertListView`, `PatientWorklistView`). Scoped
identically to `AlertListView` (`pregnancy__patient__location__organization` via
`patient__location__organization` on Pregnancy directly, plus `?assigned_to=me` via the
shared `scope_to_assigned_staff()`). A patient can appear in both queues at once; each
result still carries a `reasons` array (`["actionable"]`, `["low_confidence"]`, or both)
so a clinician can tell at a glance why a given patient is on whichever list they're
looking at.

Both still resolve through the identical `review`/`escalate`/`bulk-review` actions — no
duplicated resolution logic, only the listing duplicates (by design, minimally, via the
shared base class).

**Named and renamed three times across the day, between the two queues combined.** The
severity queue shipped first as `AttentionQueueView`/`/attention-queue/`, reusing a name a
previous session had already coined in a `PatientWorklistView` docstring for this exact
unbuilt concept. On review, that name sat oddly next to the rest of this feature and
risked being read as a rename of the separate, already-existing `Alert` model/endpoint.
Renamed to `RiskReviewQueueView`/`/risk-review-queue/`. Its confidence-only sibling first
shipped as `ConfidenceReviewQueueView`/`/confidence-review-queue/`, deliberately avoiding
"Reading Review" as a name (that phrase already means something else in Neuro_RPM — a
raw-value threshold on individual readings, not a model-confidence concept). The user then
asked directly whether "Out of Range" would fit better here instead — rejected for the
same reason `flagged_for_review` itself kept its name over `out_of_range` earlier in the
day: "out of range" means a *value* outside a bound in Neuro_RPM, and this queue's trigger
is model uncertainty, not an extreme reading — a patient here can have entirely normal
vitals. Landed on `LowConfidenceQueueView`/`/low-confidence-queue/`, the plainest literal
name for the actual trigger.

Each pagination envelope's own `count` doubles as a lightweight KPI number, the same
convention `AlertListView` already uses for its own `unacknowledged` badge — though a
proper counts-only `dashboard-kpis` endpoint was added later the same day once explicitly
requested (see "Dashboard KPIs" below), rather than requiring a full paginated fetch just
to read a number.

"Out of range" was considered and rejected as an alternate name for `flagged_for_review`
itself (raised by the user, who then asked for a recommendation): that phrase already
means something else in Neuro_RPM (a *value* outside a bound) and would misdescribe
MomCare's trigger, which fires on uncertainty, not on an extreme reading.

## Manual end-to-end testing: a real misdiagnosis, corrected, plus `pending_count`

Requested directly by the user after the split above shipped: add 10-30 real readings
across 2-3 real patients, spanning Low/Medium/High and both genuine high- and
low-confidence outputs from the actual trained model (vitals combinations were found by
probing `momcare_model.predict()` directly beforehand, so every confidence value used was
a real model output, not fabricated), and confirm both queues behave correctly through
the real API.

The run surfaced a real observation: a patient whose assessment history was Low → Medium
→ High, whose High assessment (flagged, low confidence) was reviewed via the API, **stayed
listed** in the Risk Review Queue anyway — her earlier, never-touched Medium transition
was still `pending` and still matched the severity condition on its own. This was
initially treated as a bug and "fixed" by making both queues key off only each
pregnancy's *current* (latest) assessment, annotated via `Subquery`.

**That fix was wrong, and was reverted the same day.** It contradicted a precedent already
documented earlier in this same file's own history — Neuro_RPM's
`reading_review_patient_condition()`, researched and quoted directly: *"a pending reading
from any past month still counts until it is reviewed/escalated"* — deliberately **not**
restricted to current state. The user confirmed this is exactly the intended behavior:
every Medium/High (or flagged) assessment is an independent fact needing its own
resolution; a newer transition does not silently absolve an older, still-unresolved one.
Reverted to filtering the raw `risk_assessments__` reverse relation (any pending match
counts, matching the original design), and added a `pending_count` field to each queue
result — the number of qualifying assessments still outstanding for that patient — so a
clinician can tell upfront that there's more than one to work through, whether by opening
the patient's own risk history (`GET /pregnancies/{id}/risk/`, which already lists every
assessment) and resolving each individually, or via `bulk-review`:

```python
matching_pending = RiskAssessment.objects.filter(
    self.condition(""),
    pregnancy=OuterRef("pk"),
    review_status=RiskAssessment.REVIEW_PENDING,
)
latest_relevant = matching_pending.order_by("-assessed_at")
pending_count = matching_pending.order_by().values("pregnancy").annotate(count=Count("id")).values("count")

queryset = (
    self.scope_to_organization(Pregnancy.objects.filter(status=Pregnancy.STATUS_ACTIVE))
    .filter(
        self.condition("risk_assessments__"),
        risk_assessments__review_status=RiskAssessment.REVIEW_PENDING,
    )
    .annotate(
        relevant_assessment_id=Subquery(latest_relevant.values("id")[:1]),
        pending_count=Subquery(pending_count, output_field=IntegerField()),
    )
    ...
)
```

`condition(prefix)` kept its original dual-path shape (unprefixed for the correlated
subqueries, `risk_assessments__`-prefixed for the outer `Pregnancy` filter) — the
`prefix`-less, annotated-`current_*` version from the reverted fix is gone.

**The real lesson here isn't the query mechanics — it's process.** I had already
researched and written down Neuro_RPM's exact "any past pending reading still counts"
behavior earlier in this same design doc's history, and then didn't check my own finding
against it before treating a manual test result as an obvious bug. A single test scenario
looking wrong is not sufficient grounds to change behavior — it needs to be checked
against whatever precedent or prior research already exists first.

Two tests reproduce the corrected behavior directly
(`test_risk_review_queue_keeps_a_patient_listed_for_an_older_unresolved_assessment`,
`test_low_confidence_queue_keeps_a_patient_listed_for_an_older_unresolved_assessment`):
an older pending transition matching the condition stays counted even after a newer,
already-resolved transition supersedes it. A third
(`test_risk_review_queue_reports_pending_count_across_two_outstanding_assessments`)
confirms `pending_count` counts correctly when a patient has more than one outstanding
match.

## History filters: what a doctor sees after clicking into a patient

The queues list one row per patient, matching Neuro_RPM's own patient-centric roster —
but the user pointed out the other half of Neuro_RPM's real mechanism wasn't built yet:
their frontend gets "this patient's outstanding out-of-range readings" (what a doctor
sees after clicking into a patient from the workflow) from their generic reading list's
own filters (`filterset_fields = ["patient", "reading_type", "is_out_of_range"]` plus
their own hand-rolled `review_status` alias mapping) — not a bespoke per-workflow
endpoint. `GET /pregnancies/{id}/risk/` (this project's equivalent of their reading list,
already patient/pregnancy-scoped by its own URL) had no filters at all — it returned the
patient's *entire* history unfiltered, `current` and up to 50 `history` rows, leaving a
doctor to manually scan past resolved and Low-risk entries.

Added three optional query params on `history` (never on `current`, which stays the true
current assessment regardless of any filter — clicking into a patient should still show
what's actually going on right now):

- `review_status` — `pending` | `reviewed` | `escalated`
- `actionable` — `true` (Medium/High) | `false` (Low)
- `flagged_for_review` — `true` | `false`

Clicking into a patient from the Risk Review Queue calls
`?review_status=pending&actionable=true`; from the Low Confidence Queue,
`?review_status=pending&flagged_for_review=true`. Two tests
(`test_risk_history_filters_to_pending_and_actionable_from_the_risk_review_queue`,
`test_risk_history_filters_to_pending_and_flagged_from_the_low_confidence_queue`) prove
`current` stays unfiltered while `history` narrows correctly.

## Dashboard KPIs — two endpoints, not one combined response

Explicitly requested the same day, after confirming the queue/history-filter parity
above: a counts-only surface for a doctor's dashboard, matching Neuro_RPM's own
`dashboard-kpis` convention (a separate lightweight endpoint independent of any list's
pagination) — but scoped narrowly to this feature's two queues, not a general patient
dashboard, which was declined twice before as unprompted (see `MEMORY.md`'s "Dashboard
KPIs Deferred").

First shipped as one combined `GET /risk-review-dashboard-kpis/` returning
`{"risk_review_queue": <int>, "low_confidence_queue": <int>}`. The user pointed out the
inconsistency immediately: Risk Review and Low Confidence are two separate workflows
everywhere else in this design (two queues, two conditions, two history-filter
combinations) — a single combined KPI response was the one place that split hadn't been
carried through. Split into two endpoints the same day:

- `GET /risk-review-queue-kpis/` — `{"count": <int>}`
- `GET /low-confidence-queue-kpis/` — `{"count": <int>}`

Refactored `_RiskQueueView.get()` to extract `matching_queryset(request)` — the scoped,
filtered `Pregnancy` queryset before pagination/annotation — so each KPI endpoint calls
the exact same method its own queue's `get()` uses, just `.count()`ed instead of
paginated, via a small shared `_RiskQueueKpisView` base parameterized by
`queue_view_class`. One definition of "is this pregnancy in the queue" powering both the
list and the count for each workflow, rather than a third copy that could drift from
either. Both support `?assigned_to=me`, same as both queues. Six tests (three per
endpoint): counts independently of the other workflow's own trigger, zero for a hospital
with nothing outstanding, cross-tenant isolation.

## Bulk review

`POST /risk/bulk-review/` — `{"items": [{"assessment_id", "review_status":
"reviewed"|"escalated", "confirmed_risk_level"}, ...]}`. Ported from Neuro_RPM's
`bulk_resolve_readings()`: each item resolves to its own target status in one atomic
transaction; a repeated id with a different status is a conflict, raised before anything
is touched; any single failure (not found, not accessible, not actionable, already
resolved) rolls back every write the call made. `queryset` is the caller's own
hospital-scoped `RiskAssessment` queryset — an id outside it reads as not-found, not
403, same as everywhere else in this project. Shared by both queues — an item's
`assessment_id` can come from either one.

## Permission audit: hospital_admin was missing from review/escalate/bulk-review

Requested directly by the user, separately from the feature build above: a full
role-by-role permission comparison against Neuro_RPM ("what permissions do they have for
admin, location manager, care manager, provider... implement what we're missing"). A
background research agent for the full audit failed (session limit), so this was done
directly. Most of MomCare's "system governance"-style resources (Organization settings,
Location create/reactivate, StatusLabel/ClinicalTag/NoteTemplate write access, Staff
onboarding) were already found to correctly match Neuro_RPM's real behavior — several
had already been built with an explicit admin-or-own-manager split in an earlier session
(the `locations/api/views.py` module docstring literally documents matching Neuro_RPM's
`Admin | LocationAdmin` split on purpose).

One real, confirmed gap: `review`/`escalate`/`bulk-review` used plain `IsClinician`
(Provider/Nurse/Care Manager — excludes hospital_admin), inherited from the old
`VerifyRiskView` without being re-examined. Neuro_RPM's identical action
(`review`/`escalate`/`bulk_review` on readings) uses `MANAGE = IsAdmin | IsCareManager` —
admin explicitly included. `IsClinician`'s own documented reasoning ("a hospital
administrator... is not required to have any clinical training... acknowledging [an
alert] stops the escalation ladder") was written for *Alert* acknowledgment specifically —
a concern about silencing the automated escalation clock, which doesn't apply to
`RiskAssessment.review_status`, an unrelated field with no clock of its own. Fixed by
widening to `IsClinician | IsHospitalAdmin` on `_RiskReviewActionView` (the
`review`/`escalate` base) and `RiskBulkReviewView` — additive relative to Neuro_RPM, not a
narrowing: Provider/Nurse keep the access they already had, only hospital_admin's access
was ever missing. Alert acknowledge/resolve itself is untouched, still `IsClinician`-only,
since that reasoning is still valid there specifically. Three new tests confirm
hospital_admin can review, escalate, and bulk-review.

A second real gap turned up in the same pass, outside this feature: `core/patients`'
`PatientDeactivateView`/`PatientReactivateView` had inherited the base
`PatientScopedView`'s plain `IsHospitalStaff` (correct for read/create/update, too broad
here), letting a Provider or Nurse take a patient off the active roster unilaterally.
Neuro_RPM's identical action gates to `MANAGE = IsAdmin | IsCareManager`; both MomCare
views now use `IsHospitalAdmin | IsCareManager` to match. Five new tests in
`core/patients/tests/api/test_patient_lifecycle.py` cover it (care_manager can
deactivate/reactivate; provider and nurse cannot — fault injection, asserting 403 and no
state change).

A follow-up sweep (same day) went through every area still unchecked: Staff CRUD
(profile edit/delete, deactivate/reactivate) and `SecondaryProvider` were already
confirmed correct in an earlier session (their own docstrings document the match against
Neuro_RPM). `core/monitoring`'s `MonitoringSessionDetailView`/`MonitoringNoteDetailView`
use `IsOwnerOrHospitalAdmin` on edit/delete, which matches Neuro_RPM's own
`IsOwnerOrAdmin` on both `MonitoringSessionViewSet` and `MonitoringNoteViewSet` exactly.
Device views (`DeviceListCreateView`/`DeviceAssignView`) use `IsHospitalStaff`, matching
Neuro_RPM's `DeviceViewSet`/`DeviceEnrollmentViewSet` (`IsAdmin | IsProvider |
IsCareManager | IsNurse` — the same any-staff set); one stale docstring claiming
"registering stock is an admin task" was corrected since nothing enforced that. Alert
list/detail's `IsHospitalStaff` matches the same any-staff set on Neuro_RPM's
`AlertViewSet`. No further gaps found — the two above are the complete result of the
whole audit.

## Testing

`modules/pregnancy/vitals/tests/api/test_risk.py` covers: review, escalate-is-label-only
(asserts the pregnancy's live Alert tier is untouched and no `AlertEvent` was written),
the already-resolved guard, the neither-condition guard (renamed from the original
Low-risk-only guard), a flagged-Low-can-be-reviewed case proving that fix, both queues'
lists-a-flagged-and-actionable-case / excludes-resolved / includes-its-own-trigger /
excludes-the-other-queue's-trigger / excludes-neither-condition / cross-tenant-isolation
cases, the two "keeps a patient listed for an older unresolved assessment" regression
tests, a `pending_count`-across-two-outstanding-assessments test, the two history-filter
tests, the six dashboard-KPI tests (three per endpoint), three bulk-review tests
(per-item status, all-or-nothing rollback, cross-tenant isolation), and the three
hospital_admin-can-review/escalate/bulk-review permission tests. 795 backend tests total
(whole suite), up from 767.

## Out of scope

- No Alert side effect from `escalate` (see above) — confirmed again during the queue-
  split discussion: the existing Alert/tier ladder stays completely untouched by this
  feature, now and for the foreseeable future; any integration between the two is
  explicitly deferred, not rejected.
- No `DataBound`-style configurable thresholds — the model's confidence is the only
  uncertainty trigger, matching MomCare's existing "no rules engine" decision.
- No general patient dashboard — `risk-review-dashboard-kpis` is scoped narrowly to this
  feature's two queues, explicitly requested; a broader patient dashboard was declined
  twice before as unprompted (see `MEMORY.md`).
- No duplicated resolution logic between the two queues — one `review_status`, one set
  of `review`/`escalate`/`bulk-review` actions, shared by both (see the second follow-up
  section above).
