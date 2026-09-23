# Patient Onboarding — Design

**Date:** 2026-09-15
**Status:** Approved for implementation (Part A). Part B (self-registration) is
approved as a design but is explicitly next, not this round.

## Context

MomCare already has a working patient-onboarding path (`patients/services.py::enrol_patient()`)
— location-scoped creation, mandatory consent, nested pregnancy + risk factors, MRN
generation. This document is not a green-field design; it's a deliberate revision, arrived at
by reading Neuro_RPM's own patient-onboarding implementation (`core/patients/` — models,
services, serializers, tests) as the reference for which parts are generic good practice vs.
which parts are specific to Neuro_RPM's RPM/CCM multi-program domain and don't apply to
MomCare's single maternal-health program.

Two things are being designed together because they're one story (how a patient enters
MomCare) told in two stages:

- **Part A** — a hospital directly onboarding a patient (staff-initiated). Fully finalized,
  no open questions, ready to build now.
- **Part B** — a patient self-registering via the future mobile app and requesting to join a
  hospital. Workflow is finalized; the model/request-review mechanics are specified, but
  building it is explicitly sequenced *after* Part A, not part of this implementation pass.

## Part A — Hospital-initiated onboarding

### Decision 1: Rename `enrol_patient()` → `onboard_patient()`

Pure naming alignment with Neuro_RPM's own service-function name for the same operation.
The endpoint itself is unchanged — still `POST /api/patients/` — Neuro_RPM's
`onboard_patient()` isn't a separate URL either, it's the service function `create()` calls.

### Decision 2: Keep a program/episode model — `PatientProgramEnrollment` — for future extensibility (revised 2026-09-15, after initial approval)

**Originally rejected**, on the reasoning that Neuro_RPM's `PatientProgramEnrollment` exists
only because a Neuro_RPM patient can be enrolled in several billable programs (RPM, CCM, PCM,
RTM, BHI) simultaneously, and `Pregnancy` already serves as MomCare's one enrol→outcome period
for its one program. **Reversed** on later instruction: keeping the model now, specifically so
that adding a future program (e.g. a CCM-style postpartum/chronic-care offering) is a new
`program_code` choice, not a schema redesign.

Same shape as Neuro_RPM's model, patient-scoped (not pregnancy-scoped, deliberately — so
enrollment history survives across a patient's multiple pregnancies, and so a future
non-pregnancy program wouldn't need `Pregnancy` touched at all):

| Field | Notes |
|---|---|
| `patient` (FK) | Not `pregnancy` |
| `program_code` | Choices field. Only one value defined for now: `"rpm"` |
| `status` | enrolled / paused / discharged |
| `enrolled_at` / `disenrolled_at` | |
| Constraint | At most one **open** enrollment per (patient, program_code) — partial unique constraint, same as Neuro_RPM |

This sits *alongside* `Pregnancy`, not instead of it — `Pregnancy` remains the clinical/dating
record; `PatientProgramEnrollment` is the administrative "is she actively in this program"
state with history, decoupled from any one pregnancy's own lifecycle.

**Program code naming — `"rpm"`, not `"ob_rpm"`.** Checked against real industry usage:
"OB-RPM"/"Maternal RPM" is a genuine term (maternal-health remote monitoring, covering
conditions like preeclampsia and gestational diabetes), but it uses the exact same underlying
CPT billing codes as generic RPM (99453/99454/99457/99458) — it isn't a separate program
category, just a domain label for "RPM applied to obstetric patients." Since MomCare is a
global product and OB-RPM's meaning is rooted in US Medicare/Medicaid billing terminology that
doesn't travel to other countries, the internal `program_code` slug stays `"rpm"` (matches
Neuro_RPM, invisible to end users), and the **display label** anyone actually sees is
`"Maternal Monitoring"` — plain, globally understandable, carries no US-billing assumption.
Implemented directly as the Django choices tuple (`("rpm", "Maternal Monitoring")`), no
separate mapping needed.

Onboarding payload gains an explicit `program_enrollments` block (matching Neuro_RPM's own
request shape), rather than auto-opening the enrollment silently — deliberate, so that adding a
second program later requires no special-casing of "the implicit first one."

### Decision 3: Full care team via three direct FK columns, not a join table

**Chosen: match Neuro_RPM's model exactly** — `provider`, `nurse`, `care_manager` as three
separate FK columns on `Pregnancy`, one-of-each-at-a-time.

**Considered and rejected:** keeping/extending MomCare's existing `CareTeamMembership` join
table (many-to-many via join rows, supports multiple simultaneous nurses on rotation, full
handoff history via `started_at`/`ended_at`). This is a real capability the three-FK model
gives up — a pregnancy can only ever have one nurse and one care manager at a time, and
reassigning someone silently overwrites who was there before, with no history. Explicitly
decided against anyway, in favor of matching Neuro_RPM's simpler shape. `CareTeamMembership`
is retired entirely: the model, its `/pregnancies/{id}/care-team/...` endpoints, and its
tests are deleted, not deprecated-in-place.

`Pregnancy.assigned_staff` is renamed to `Pregnancy.provider` (same field, same FK target,
new name — matches `care_team.provider` in the onboarding payload and Neuro_RPM's naming).
`nurse` and `care_manager` are new FK columns, same shape (`FK → Staff, nullable`).

**On-delete diverges from Neuro_RPM deliberately:** all three columns use `PROTECT`, not
Neuro_RPM's `SET_NULL`. This matches MomCare's existing rule (already true of the pre-rename
`assigned_staff`, and of `ClinicalNote.author`) that a staff member is soft-deactivated, never
hard-deleted while they still have protected clinical history — `PROTECT` is what makes that
rule real at the database level.

### Decision 4: Care-team assignment validation

Every assignment to `provider`, `nurse`, or `care_manager` — whether at onboarding time or via
a later update — is checked against three rules, mirroring Neuro_RPM's `assign_care_team()`:

1. **Role match** — the assigned `Staff` row's `role_code` must equal the slot (a `Staff` with
   role `nurse` cannot fill the `provider` slot). This closes a real gap found in the current
   `CareTeamMembership.role` field, which is *never* validated against the assigned staff
   member's actual role today.
2. **Org match** — the assigned staff member must belong to the same organization as the
   patient.
3. **Capacity** — the assigned staff member must not already be at `Staff.max_patients`
   (checked with a row lock, so two concurrent assignments can't both squeak past the limit).

All three enforced in one service function, called from both the onboarding payload's nested
`care_team` block and any later care-team update — there is exactly one code path that can
write these three fields, never a direct `.save()`. "Later update" means the existing
`PATCH /api/patients/{id}/pregnancies/{pregnancy_id}/` endpoint accepting `provider`/`nurse`/
`care_manager` directly — no new dedicated care-team endpoint is added; the old
`/pregnancies/{id}/care-team/...` endpoints are simply removed (Decision 3), and this existing
pregnancy-update endpoint takes over the "reassign care team later" job.

### Decision 5: `Patient` gains a direct `organization` column

Needed to correctly scope the new CNIC-uniqueness rule (Decision 6) — `Patient` currently only
reaches its organization *indirectly*, through `location.organization`, and since one hospital
can have several locations, a location-scoped constraint would not catch a duplicate CNIC at a
different branch of the same hospital. `Device` already carries a direct `organization` FK for
exactly this kind of reason — this follows that existing precedent rather than inventing a new
one. `PROTECT`, matching the rest of tenant-owned models in this codebase.

### Decision 6: CNIC unique per organization; phone stays non-unique

**Considered:** applying Neuro_RPM's NULL-not-empty-string uniqueness discipline uniformly to
both phone and CNIC (Neuro_RPM's `phone`/`username` are unique fields on `User`).

**Rejected for phone specifically.** MomCare's `phone` field is deliberately non-unique today
— documented reason: rural households commonly share one phone across several patients (e.g.
two sisters-in-law under one husband's number). Making phone unique would outright block
onboarding the second patient. Phone stays exactly as it is: indexed, searchable, not unique.

**Accepted for CNIC.** A CNIC is a personal government ID — one person, one CNIC, by design.
Two different patients sharing one CNIC value is much more likely to be a data-entry mistake
than a legitimate case, so catching it is a real win, not a false positive risk. New
constraint: `UniqueConstraint(fields=["organization", "cnic"], condition=Q(cnic__isnull=False))`.
CNIC is stored as `NULL`, never `""`, when absent — the same discipline Neuro_RPM uses for its
own optional-but-unique fields — so two CNIC-less patients at the same hospital never
false-positive collide.

### Decision 7: No fuzzy duplicate-patient detection

Considered adding a soft, non-blocking warning when phone/CNIC/name+DOB looks like an existing
patient. Rejected on inspection of what Neuro_RPM actually does: its "strict rules" are plain
database uniqueness constraints (`email`, `phone`, `username`, `MRN` — hard rejection on
collision), nothing fuzzy. Decision 6's CNIC constraint already **is** that same rule, applied
to the field that makes sense for MomCare. A fuzzy-match feature would be new, untested surface
area Neuro_RPM itself doesn't have — YAGNI.

### Decision 8: No bulk onboarding

Explicitly out of scope — no stated requirement for it, Neuro_RPM doesn't have it either.

### Decision 9: `emergency_contact_email` added; no caregiver login account

Considered giving the emergency contact a full platform account — a new role, org-scoped-to-
one-patient permissions, credentials emailed the same way staff onboarding already works.
**Rejected.** `Patient` already has `emergency_contact_name`/`_phone`/`_relation`; the contact
is not a new kind of system user, just better-captured data on the existing model. One field is
added, `emergency_contact_email` (nullable), captured now for a future notification feature
that is explicitly not being built this round — no send-logic, no new role, no login.

### Decision 10: No delivery-plan/distance/danger-sign-counseling fields

These were proposed (grounded in general maternal-mortality "three delays" reasoning) and then
explicitly declined — emergency contact capture is considered sufficient for this round.
Recorded here so they aren't silently reconsidered later without it being a deliberate choice.

### Final schema — Part A

**`Patient`** (★ = new or changed)

| Field | Type | Notes |
|---|---|---|
| id | UUID PK | |
| ★ organization | FK → Organization, PROTECT | new — Decision 5 |
| location | FK → Location, PROTECT | unchanged |
| user | FK → User, SET_NULL, nullable | unchanged |
| first_name, last_name, date_of_birth, gender | — | unchanged |
| phone | not unique | unchanged — Decision 6 |
| ★ cnic | NULL when absent | new constraint: unique with `organization` — Decision 6 |
| blood_group | choice | unchanged |
| emergency_contact_name / _phone / _relation | — | unchanged |
| ★ emergency_contact_email | nullable | new — Decision 9 |
| mrn | unique, org-prefixed | unchanged |
| is_active / deactivated_* | — | unchanged |

**`Pregnancy`** (★ = new or changed)

| Field | Type | Notes |
|---|---|---|
| id, patient, lmp, edd, edd_source, edd_confirmed_at, gravida, para | — | unchanged |
| ★ provider | FK → Staff, PROTECT, nullable | renamed from `assigned_staff` — Decision 3 |
| ★ nurse | FK → Staff, PROTECT, nullable | new — Decision 3 |
| ★ care_manager | FK → Staff, PROTECT, nullable | new — Decision 3 |
| status, outcome_date, notes | — | unchanged |

**`PregnancyRiskFactors`, `Consent`** — unchanged.

**`CareTeamMembership`** — ★ deleted (model, endpoints, tests) — Decision 3.

**★ `PatientProgramEnrollment`** — new — Decision 2 (revised)

| Field | Type | Notes |
|---|---|---|
| id | UUID PK | |
| patient | FK → Patient, PROTECT | not `pregnancy` — survives across her pregnancies |
| program_code | choice, only `"rpm"` defined (display: "Maternal Monitoring") | new choices added later, no schema change |
| status | enrolled / paused / discharged | |
| enrolled_at / disenrolled_at | | |
| — | — | constraint: at most one open enrollment per (patient, program_code) |

### Onboarding flow

`POST /api/patients/`, `onboard_patient()`, one atomic transaction:

1. Create `Patient` (organization + location resolved server-side from the caller, never
   client-supplied).
2. Record `Consent` (mandatory, as today).
3. If `pregnancy` block supplied: create `Pregnancy` (requires at least `lmp` or `edd`) +
   `PregnancyRiskFactors`.
4. If `care_team` block supplied within the pregnancy: assign `provider`/`nurse`/
   `care_manager`, each through the Decision 4 validation. Optional — a patient can be
   onboarded without a full care team and staffed later via the same validation path.
5. If `program_enrollments` block supplied: open a `PatientProgramEnrollment` for each entry
   (today, only `program_code: "rpm"` is valid).
6. MRN allocated with existing retry-on-collision logic (unchanged).

### Migration notes (for the implementation plan, not decided here)

- `Patient.organization` backfilled from `location.organization` for existing rows before the
  column is made non-nullable.
- `Pregnancy.assigned_staff` renamed to `provider` (same column, no data movement).
- Existing `CareTeamMembership` rows: no migration path preserves them as multi-member
  history, since the target schema has no equivalent — this is an accepted, deliberate loss
  consistent with Decision 3, not an oversight.
- CNIC uniqueness constraint addition needs a pre-check for existing duplicate CNICs within an
  organization before the constraint can be applied.
- `PatientProgramEnrollment` is a brand-new table — existing patients get a backfilled open
  `"rpm"` enrollment (`enrolled_at` = their `Patient.created_at`) so pre-existing records aren't
  left without one.

## Part B — Self-registration and hospital join-request

### Decision 11: Self-registered accounts start with no organization

A patient using the future mobile app registers directly — name, DOB, phone, email, password
— with **no hospital attached**. This creates only a `User` row, `role=patient`,
`organization=NULL`. Not a new pattern: `platform_admin` users already have `organization=NULL`
in this system, for the same reason (not tied to one hospital).

### Decision 12: Self-reported clinical data is a draft, not a real `Patient`/`Pregnancy`

She can fill in the same data a hospital would capture — pregnancy dating, gravida/para,
risk-factor checklist, consent — before ever choosing a hospital. This is held as **draft
data on the join request** (Decision 13), not written into `Patient`/`Pregnancy`. Reason:
those tables' whole invariant is "belongs to exactly one hospital" — creating a real,
organization-less `Patient` row would break that invariant and reopen the RLS fail-closed
question for no benefit, since nothing can act on that data clinically until a hospital is
actually attached.

### Decision 13: `PatientJoinRequest` — same review-gate shape `Organization` already uses

A new model: patient (`User` FK), target `organization` (FK), the draft data from Decision 12,
`status` (pending/approved/rejected), `requested_at`, `decided_at`, `decided_by`. This isn't a
new pattern for MomCare — `Organization` registration itself already goes through exactly this
pending→approved→rejected review gate. Org-scoped via the normal `organization` column and the
existing scoping mixins — each hospital only ever sees requests addressed to itself; this is
*not* a cross-tenant view (unlike `platform_admin`'s deliberate exception for
`OrganizationDeactivationRequest`).

### Decision 14: Approval reuses the Part A onboarding mechanism — this is the important part

Approving a request is not a separate creation path. Staff review the draft data (can correct
it), supply the two things only they can decide — `location` and the `provider`/`nurse`/
`care_manager` care team — and that triggers the *same* `onboard_patient()`-style creation used
for a walk-in. This is what prevents duplicate patient identities: there is exactly one place
`Patient`/`Pregnancy` rows get created, regardless of whether the trigger was a staff walk-in or
an approved remote request.

Rejection leaves the `User` account and its draft data untouched — she can request a different
hospital.

### Decision 15: Hospital directory is a filterable list, not GPS search

Considered real distance-based "hospitals near me" search. `Location` today has a structured
address (via `AddressMixin`) but no latitude/longitude — building real geo-search means new
location infrastructure for a feature with no confirmed need yet. **Chosen:** a simple list,
filterable by city/region using the address fields that already exist. Upgradable to real geo
search later if patients actually need it — not designed against, just not built now.

### Decision 16: How/whether a hospital contacts the patient before approving is out of scope

Whether a hospital calls her, asks her to visit in person, or approves purely by reviewing the
submitted draft data is entirely the hospital's own operational process. The system's
responsibility ends at: hold the pending request, show the draft data, provide approve/reject
with the location/care-team fields Decision 14 needs. Not enforced or assumed either way.

### Explicitly out of scope (Part B, this pass)

- Any patient-facing clinical view/portal after her account exists — she can manage her draft
  and pick a hospital, nothing else. A separate, unbuilt feature.
- GPS-based distance search (Decision 15).
- Automatic merging if the same person is later onboarded as a walk-in *before* her pending
  request is decided — not addressed by this design; flagged here so it isn't silently assumed
  to be handled.

## Summary of what's explicitly not being built (both parts)

- Bulk/CSV patient onboarding.
- Fuzzy duplicate-patient detection.
- A caregiver/emergency-contact login account of any kind.
- Delivery-plan, distance-to-facility, or danger-sign-counseling fields.
- GPS-based hospital search.
- A patient-facing portal/app view beyond what Part B's registration flow itself needs.
