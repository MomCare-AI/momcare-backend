# Patient Profile and Join Request — Design

**Date:** 2026-10-07
**Status:** Implemented (backend + API + both Postman collections). Replaces the "draft + one-click approve" flow in
`2026-09-15-patient-onboarding-design.md`.
**Terminology:** "hospital" and "organization" mean the same thing here (`Organization` = one hospital).

## Context

A woman signs herself up in the Flutter app, then asks a hospital to take her on. Until now the join request carried a
"draft" she typed per hospital (name again, medical history, pregnancy dating) and a hospital staff member
approved it with one click, which created the patient straight from that draft.

Problems: she retyped everything for every hospital; she was asked medical questions she cannot answer reliably;
and approval gave the hospital no chance to add what only a clinician knows (MRN, care team, pregnancy, conditions).

## Decisions

| # | Decision |
|---|---|
| 1 | She enters her details **once**, in a profile that belongs to her account, not to any hospital. |
| 2 | The profile is **identity only**. No medical history, no pregnancy dating, no allergies. Those belong to the clinician at the visit. |
| 3 | Most of the profile already lives on `User` (name, phone, DOB, address, language). Only what `User` lacks goes in a new table, `PatientProfile`. One source of truth per fact. |
| 4 | A join request carries **only the hospital** (and optionally one of its branches). The server copies her profile into the request as a **frozen snapshot** at send time. |
| 5 | She cannot send a request until her profile is **complete**. The API refuses with the list of missing fields. |
| 6 | The hospital opens a request in the **existing Patient Onboarding screen**, pre-filled from the snapshot. Staff complete the clinical fields. **Saving that form is the approval**: `POST /patients/` with `join_request`. There is no separate Approve endpoint. |
| 7 | A request stays **pending** until staff save the form, for as long as it takes her to visit. |
| 8 | The patient lands in the **branch she asked for** (the request's `location`), not the hospital default. If she named none, the hospital default is used. Maps / nearby search are future work. |
| 9 | Gender is removed from `Patient` (the app is for women). `User.gender` stays: it is shared with staff. |
| 10 | `cnic` is renamed `national_id` everywhere on the patient side (API, DB column, per-hospital uniqueness). It is optional: not every country has one. |
| 11 | Last name is required at patient sign-up. |
| 12 | Consent to share her profile is the act of sending the request; the clinical consent stays staff-entered (`consent_date`) at the visit. No consent tick-box. |

## Data

### New: `PatientProfile` (`patients_patientprofile`)

One row per patient account, one-to-one with `User`. No hospital column: it is not tenant data.

| Field | Type | Required to send a request |
|---|---|---|
| `user` | one-to-one `User` | n/a |
| `national_id` | text(20), nullable | no (country dependent) |
| `blood_group` | A+ A− B+ B− AB+ AB− O+ O−, blank allowed | no |
| `emergency_contact_name` | text(100) | yes |
| `emergency_contact_phone` | text(20) | yes |
| `emergency_contact_relation` | text(50) | yes |
| `emergency_contact_email` | email | yes |

The row is created the first time she saves her profile.

### Read from `User` (no new column)

`first_name`, `last_name` (both required), `phone` (required, unique, blank stored as `NULL`), `date_of_birth`
(required), the six address fields (required, already collected at sign-up), `email` (read-only).

### Complete means

`first_name`, `last_name`, `phone`, `date_of_birth`, all six address fields, and the four emergency-contact fields are
all non-blank. `national_id` and `blood_group` are optional.

### Changed tables

| Table | Change |
|---|---|
| `patients_patient` | `cnic` → `national_id` (column, index, `unique_national_id_per_organization`); `gender` dropped |
| `patients_patientjoinrequest` | new nullable `location` FK (`SET_NULL`); `draft` JSON now holds the server-built profile snapshot |

`PatientProfile` has no Row-Level Security policy: it has no `organization` to scope on, exactly like `User`. Only the
account owner reads or writes it, through an explicit `user=request.user` filter. Hospital staff never read it: they see
the frozen snapshot on the (RLS-protected) join request.

## Flow

```
register (name, email, password, phone?, address)  ->  verify email OTP  ->  login
GET /my-profile/               is_complete=false, missing_fields=[...]
PATCH /my-profile/             DOB, phone, emergency contact, ...
GET /hospitals/                hospitals, each with its active branches
POST /my-requests/             { organization, location? }   400 if profile incomplete, 409 if already pending
        ...request waits in the hospital's join-request list...
GET /patient-requests/{id}/    request + snapshot (pre-fills the onboarding screen)
POST /patients/                same onboarding form + join_request id  ==  approval
        -> Patient created in the requested branch, her login linked, request approved,
           her other pending requests withdrawn
POST /patient-requests/{id}/reject/   unchanged
```

`POST /patients/` with a `join_request` runs in one transaction. It refuses (404) a request that is not this
hospital's, (409) one already decided, and (409) a woman already onboarded by another hospital. Staff-entered values
always win over the snapshot, because the snapshot only pre-fills the form.

A patient with no pregnancy is valid. Onboarding happens at the visit with whatever is known; LMP/EDD, the seven
history answers and the care team can be added or corrected later (for example after an ultrasound) through the
existing pregnancy endpoints. Until a pregnancy exists she has no readings or care plan.

## Endpoints

### Mobile collection

| | Endpoint |
|---|---|
| new | `GET /my-profile/`, `PATCH /my-profile/` |
| changed | `GET /hospitals/` (adds `locations`), `POST /my-requests/` (body is `{organization, location?}`), `GET /auth/me/` (patient accounts also get `profile_complete`) |
| unchanged | register, verify-email, resend-verification, login/refresh/logout, password endpoints, `GET /my-requests/`, withdraw, current care plan |

### Web collection

| | Endpoint |
|---|---|
| new | `GET /patient-requests/{id}/` |
| changed | `POST /patients/` (optional `join_request`; `cnic` is now `national_id`; no `gender`) |
| removed | `POST /patient-requests/{id}/approve/` |
| unchanged | `GET /patient-requests/`, `POST /patient-requests/{id}/reject/` |

The request representation now exposes the snapshot as `profile` (it was `draft`) plus `location` / `location_name`.

## Addendum (8 Oct 2026): what the patient reads of her care plan

Strictly read-only: a patient never creates or changes anything in a care plan. She sees the plan, nutrition,
exercise, the doctor's medications and notes, and her earlier months.

| Endpoint | Notes |
|---|---|
| `GET /pregnancies/{id}/current-care-plan/` | existing; whole plan, patient version (no staff bookkeeping) |
| `.../current-care-plan/nutrition/`, `.../exercise/` | existing |
| `.../current-care-plan/medications/`, `.../notes/` | new; `care_plan.items = [{id, text, added_by, created_at}]`, removed items hidden; `care_plan` is `null` until a plan exists; staff may use them too |
| `GET /my-care-plans/`, `GET /my-care-plans/{id}/` | new; her own plans, every month, patient version; paginated; the staff routes `/care-plans/` stay staff-only |

**Replies to notes were considered and rejected.** A one-way reply to the doctor has no clinician watching it: a woman
could report a symptom there and be unheard. Urgency goes through readings and alerts, the plan's contact-your-care-team
message and warning signs; real two-way messaging would be its own designed feature.

## Addendum (8 Oct 2026): what the patient reads of her readings and risk

Strictly read-only. **A patient cannot record a reading** -- manual entry stays on the hospital side, and device
readings come from the device. The staff routes stay staff-only; she gets her own `my-` routes under her pregnancy
(`pregnancy_id` comes from `GET /auth/me/`), scoped to her hospital and then to herself.

| Endpoint | Notes |
|---|---|
| `GET /pregnancies/{id}/my-readings/` | newest first, standard envelope; `period` (2_days, 1_week, 1_month, 3_months, 6_months, 1_year), `start_date`+`end_date`, `since`; `reading_type` adds `statistics` (average/min/max/categories). Same helper (`readings_in_window`) as the staff list, so the numbers can never differ. The internal `device` id is hidden |
| `GET /pregnancies/{id}/my-readings/latest/` | `reading` is `null` when there is none |
| `GET /pregnancies/{id}/my-vitals-summary/` | 30-day averages + this month's low/medium/high split |
| `GET /pregnancies/{id}/my-risk/` | `current` + history (default last 30 days, `period` takes the same codes, max 100). `contact_care_team` / `contact_message` when current is high, and a `notice` that it is an automatic check, not a diagnosis |

She sees `final_risk_level` and the per-vital categories, **never** the model's raw answer, confidence,
flagged-for-review, review status, who confirmed it or the staff filters (those query params are ignored).

**The AI summary stays staff-only**: it is written for clinicians from staff-only data (monitoring notes, statuses, risk
review). Her own written content is the care plan's progress text and quick advice.

Also fixed while building this: the test settings write care plans inline, so every test that saved a reading called the
AI provider for real with the key from `.env`. `generate_researched` is now blocked in the root autouse fixture.

## Not built (on purpose)

- Maps, "hospitals near me", automatic branch choice from her position (needs coordinates on `Location`).
- A consent-form template/record feature and report/ultrasound uploads (needs file storage).
- Per-branch visibility of requests for branch staff.
